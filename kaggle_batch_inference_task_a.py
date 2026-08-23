# %% [code]
# This Python 3 environment comes with many helpful analytics libraries installed
# It is defined by the kaggle/python Docker image: https://github.com/kaggle/docker-python

import os
import sys
import json
import time
import requests
from io import BytesIO
from PIL import Image
from tqdm.auto import tqdm
import torch

# %% [code]
# ==============================================================================
# AUTO-INJECTED WORKLOAD SHARD CONFIGURATION (Defaults for Standalone Run)
# ==============================================================================
# When running via the distributed runner, these are injected at the top of the file.
# When running standalone, they fall back to environment variables or defaults.
def _get_shard_var(name, default=None, cast=None):
    """Read a shard variable from globals (injected) or env, with a fallback default."""
    val = globals().get(name)
    if val is not None:
        return val
    env_val = os.environ.get(name)
    if env_val is not None and env_val.strip():
        # cast=int is critical for index vars: raw strings from env would crash
        # slicing (TypeError) and break range comparisons downstream.
        if cast is not None:
            try:
                return cast(env_val)
            except (TypeError, ValueError):
                return default
        if default is not None:
            return type(default)(env_val)
        return env_val
    return default

SHARD_ID = _get_shard_var('SHARD_ID', 0, cast=int)
TOTAL_SHARDS = _get_shard_var('TOTAL_SHARDS', 1, cast=int)
START_INDEX = _get_shard_var('START_INDEX', None, cast=int)
END_INDEX = _get_shard_var('END_INDEX', None, cast=int)

print("=" * 60)
print(f"SHARD CONFIG: Shard {SHARD_ID + 1}/{TOTAL_SHARDS} (Index range: {START_INDEX} -> {END_INDEX})")
print("=" * 60)

# %% [code]
# 1. Install & Upgrade required libraries for Vision-Language Inference on Kaggle
import subprocess

def run_cmd(cmd, description):
    """Run a shell command and exit on failure."""
    print(f"{description}...")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[ERROR] {description} failed (rc={result.returncode}):")
        out = (result.stdout or '')[-300:]
        err = (result.stderr or '')[-300:]
        if out: print(f"STDOUT: {out}")
        if err: print(f"STDERR: {err}")
        sys.exit(1)
    return result

USE_UV = subprocess.run("uv --version", shell=True, capture_output=True).returncode == 0

def install_cmd(args):
    """uv requires --system when no venv is active (Kaggle has none); pip gets -q."""
    return f"uv pip install --system {args}" if USE_UV else f"pip install -q {args}"

# --no-deps for heavy libs so uv/pip never re-resolves Kaggle's pre-installed CUDA torch;
# their lightweight runtime deps are installed explicitly right after.
run_cmd(install_cmd("--no-deps accelerate bitsandbytes qwen-vl-utils"),
        "Installing inference deps without touching pre-installed torch")
run_cmd(f"{install_cmd('--no-deps')} git+https://github.com/huggingface/transformers.git",
        "Installing transformers from source (for Qwen3.5 MoE support)")
run_cmd(install_cmd("'tokenizers>=0.23.1' 'safetensors>=0.8.0' 'huggingface_hub>=0.30.0' 'requests>=2.32.0' 'regex' 'tqdm' 'pyyaml' 'filelock' 'fsspec' 'packaging' 'numpy' 'psutil' 'pillow' 'hf_transfer'"),
        "Installing transformers/accelerate runtime dependencies (excluding torch)")

# Multi-connection model downloads (same idea as aria2c -x16, native to HF Hub).
# Falls back to plain downloads if the package is missing.
try:
    import hf_transfer  # noqa: F401
    os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"
    print("Fast model downloads enabled via hf_transfer")
except ImportError:
    print("hf_transfer unavailable - using standard HF downloads")

run_cmd("wget -q -N https://huggingface.co/datasets/barryallen16/fitcheck-annotate-dataset/resolve/main/task_a_dataset.jsonl",
        "Downloading Task A dataset from HuggingFace")

from transformers import AutoModelForImageTextToText, AutoProcessor, BitsAndBytesConfig
from qwen_vl_utils import process_vision_info

# %% [code]
# 2. Configuration & Paths
MODEL_ID = "Qwen/Qwen3.6-27B"

# /kaggle/working only exists on Kaggle; fall back to CWD elsewhere
WORKING_DIR = "/kaggle/working" if os.path.exists("/kaggle/working") else os.getcwd()

# Automatically detect input dataset path
INPUT_FILE = os.path.join(WORKING_DIR, "task_a_dataset.jsonl")
if not os.path.exists(INPUT_FILE):
    INPUT_FILE = "task_a_dataset.jsonl"
if not os.path.exists(INPUT_FILE):
    for root, _, files in os.walk("/kaggle/input"):
        for file in files:
            if file.endswith("task_a_dataset.jsonl"):
                INPUT_FILE = os.path.join(root, file)
                break
        if os.path.exists(INPUT_FILE):
            break

# Dynamic output file naming per shard for clean parallel aggregation
if TOTAL_SHARDS > 1:
    OUTPUT_FILE = os.path.join(WORKING_DIR, f"task_a_labeled_shard_{SHARD_ID}.jsonl")
else:
    OUTPUT_FILE = os.path.join(WORKING_DIR, "task_a_labeled.jsonl")

CHECKPOINT_INTERVAL = 25  # Flush progress to disk every N items

print(f"Input file path:  {INPUT_FILE}")
print(f"Output file path: {OUTPUT_FILE}")
print(f"Active GPU count: {torch.cuda.device_count()}")
for i in range(torch.cuda.device_count()):
    print(f"  GPU {i}: {torch.cuda.get_device_name(i)} ({torch.cuda.get_device_properties(i).total_memory / (1024**3):.1f} GB)")

# %% [code]
# 3. Load Teacher Model in 4-bit Across Dual T4 GPUs
print(f"\nLoading {MODEL_ID} with 4-bit quantization across dual T4 GPUs...")

# GPU memory check before loading
gpu_count = torch.cuda.device_count()
if gpu_count == 0:
    print("[FATAL] No GPU detected. This script requires a GPU accelerator.")
    sys.exit(1)

total_vram_gb = sum(torch.cuda.get_device_properties(i).total_memory for i in range(gpu_count)) / (1024**3)
print(f"Total available VRAM: {total_vram_gb:.1f} GB across {gpu_count} GPU(s)")
if total_vram_gb < 28:
    print(f"[WARNING] Only {total_vram_gb:.1f} GB VRAM available. Model may OOM.")
    print("         Recommendation: Use NvidiaTeslaT4 (2x T4 = 32 GB) or higher.")

bnb_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_compute_dtype=torch.float16,
    bnb_4bit_use_double_quant=True
)

# FIX: Qwen3.6-35B-A3B is an image-text-to-text model whose architecture class is
# Qwen3_5MoeForConditionalGeneration. AutoModelForCausalLM cannot load it
# (not registered under the causal-LM mapping) -> use AutoModelForImageTextToText.
#
# FIX: bare device_map="auto" under-budgets each T4 and strands modules on CPU,
# which bnb 4-bit rejects ("Some modules are dispatched on the CPU or the disk").
# Give accelerate an explicit per-GPU budget and allow the few non-quantizable
# modules (MoE router gates etc.) to sit in fp32 on CPU.
# ~14.6 GiB physical per T4. Reserve ~1.5 GiB per GPU for activations + KV cache
# during generation - packing weights to the brim causes OOM inside SDPA attention.
gpu_mem_budget = {i: f"{int(torch.cuda.get_device_properties(i).total_memory / (1024**3)) - 1}.5GiB" for i in range(gpu_count)}
print(f"Memory budget: {gpu_mem_budget}")

# Reduce fragmentation for the many large allocations during load/inference
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

model = AutoModelForImageTextToText.from_pretrained(
    MODEL_ID,
    quantization_config=bnb_config,
    device_map="auto",  # Automatically shards across available GPUs
    max_memory=gpu_mem_budget,
    trust_remote_code=True
)

# Hard-bound the visual token budget: Qwen VL expands each image into
# ~(pixels / 28*28) tokens. Capping max_pixels at 768*28*28 (~750 tokens/img)
# keeps a 4-image prompt within ~3k visual tokens so prefill fits T4 VRAM.
processor = AutoProcessor.from_pretrained(
    MODEL_ID,
    trust_remote_code=True,
    min_pixels=256 * 28 * 28,
    max_pixels=768 * 28 * 28
)
print("Model and processor successfully loaded across GPUs!")

# %% [code]
# 4. Helper Functions: Image Fetching & JSON Parsing
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def fetch_image(url: str, timeout: int = 8, retries: int = 2) -> Image.Image:
    """Fetch an image from URL and convert to RGB PIL Image with size capping."""
    img = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            if resp.status_code == 200:
                img = Image.open(BytesIO(resp.content)).convert("RGB")
                # 768px cap: 4 images/item at 1024px produced thousands of visual
                # tokens -> prefill OOM on T4s. 768 keeps quality with ~44% less.
                max_dim = 768
                if max(img.size) > max_dim:
                    img.thumbnail((max_dim, max_dim))
                return img
        except Exception:
            pass
        # Back off between attempts regardless of failure mode (non-200 included)
        if attempt < retries - 1:
            time.sleep(1)
    return img

def extract_json_response(raw_text: str) -> dict:
    """Extract and parse clean JSON from the VLM output."""
    raw_text = raw_text.strip()
    # Strip any reasoning blocks that slipped through
    while "<think>" in raw_text and "</think>" in raw_text:
        raw_text = raw_text.split("</think>", 1)[1].strip()
    if "```json" in raw_text:
        raw_text = raw_text.split("```json")[1].split("```")[0].strip()
    elif "```" in raw_text:
        raw_text = raw_text.split("```")[1].split("```")[0].strip()
    
    try:
        return json.loads(raw_text)
    except Exception:
        start = raw_text.find("{")
        end = raw_text.rfind("}")
        if start != -1 and end != -1:
            try:
                return json.loads(raw_text[start:end+1])
            except Exception:
                pass
    return {"raw_output": raw_text}

# %% [code]
# 5. Dataset Shard Slicing & Resumability Check
all_items = []
with open(INPUT_FILE, "r", encoding="utf-8") as f:
    for line in f:
        if line.strip():
            all_items.append(json.loads(line))

total_items = len(all_items)
print(f"Total dataset items found: {total_items}")

# Determine shard partition range
if START_INDEX is not None and END_INDEX is not None:
    # Explicit start/end range passed by sharder or smoke-run override.
    # Python slicing clamps safely, so equal/degenerate ranges yield empty shards.
    shard_items = all_items[START_INDEX:END_INDEX]
elif TOTAL_SHARDS > 1:
    # Split evenly based on SHARD_ID and TOTAL_SHARDS
    items_per_shard = (total_items + TOTAL_SHARDS - 1) // TOTAL_SHARDS
    s_idx = SHARD_ID * items_per_shard
    e_idx = min(s_idx + items_per_shard, total_items)
    shard_items = all_items[s_idx:e_idx]
    print(f"Computed slice for Shard {SHARD_ID}: {s_idx} to {e_idx}")
else:
    shard_items = all_items

print(f"Total items assigned to this shard: {len(shard_items)}")

# Check for existing progress in this shard file
processed_ids = set()
if os.path.exists(OUTPUT_FILE):
    with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                data = json.loads(line)
                if "id" in data:
                    processed_ids.add(data["id"])
            except Exception:
                pass

print(f"Found {len(processed_ids)} already processed items in {OUTPUT_FILE}.")

items_to_process = [item for item in shard_items if item.get("id") not in processed_ids]
print(f"Remaining items to process on this shard: {len(items_to_process)}")

# %% [code]
# 6. Run Batch Inference Loop on Assigned Shard
out_handle = open(OUTPUT_FILE, "a", encoding="utf-8")
success_count = 0
failed_count = 0

start_time = time.time()

try:
    for item in tqdm(items_to_process, desc=f"Shard {SHARD_ID} [{MODEL_ID}]"):
        item_id = item["id"]
        img_urls = item.get("images", [])
        prompt_text = item.get("prompt", "")
        
        # 1. Download multi-angle images (capped to 2-4 per handoff §1)
        pil_images = []
        for url in img_urls[:4]:
            img = fetch_image(url)
            if img is not None:
                pil_images.append(img)
                
        if not pil_images:
            print(f"\n[Warning] Could not load images for {item_id}, skipping.")
            failed_count += 1
            continue
        
        # 2. Build multimodal message structure
        content = []
        for img in pil_images:
            content.append({"type": "image", "image": img})
        content.append({"type": "text", "text": prompt_text})
        
        messages = [{"role": "user", "content": content}]
        
        # 3. Format inputs with processor
        # enable_thinking=False: Qwen3+ emits chain-of-thought by default, which
        # burned the token budget on reasoning text before any JSON appeared.
        try:
            text_prompt = processor.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True,
                enable_thinking=False
            )
        except TypeError:
            text_prompt = processor.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
        image_inputs, video_inputs = process_vision_info(messages)
        
        inputs = processor(
            text=[text_prompt],
            images=image_inputs,
            videos=video_inputs,
            padding=True,
            return_tensors="pt"
        ).to(model.device)
        
        # 4. Generate response
        with torch.no_grad():
            generated_ids = model.generate(
                **inputs,
                max_new_tokens=768,  # room for full JSON even if some reasoning slips through
                do_sample=False  # Deterministic output for structured JSON
            )
            
        generated_ids_trimmed = [
            out_ids[len(in_ids):] for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
        ]
        response_text = processor.batch_decode(
            generated_ids_trimmed,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False
        )[0]
        
        parsed_json = extract_json_response(response_text)
        
        # 5. Build labeled record
        labeled_record = {
            "id": item_id,
            "url": item.get("url", ""),
            "images": img_urls,
            "teacher_output": parsed_json,
            "grounding_metadata": item.get("grounding_metadata", {})
        }
        
        out_handle.write(json.dumps(labeled_record, ensure_ascii=False) + "\n")
        success_count += 1
        
        if success_count % CHECKPOINT_INTERVAL == 0:
            out_handle.flush()
            
except KeyboardInterrupt:
    print("\nInference paused by user. Progress safely saved!")
finally:
    out_handle.flush()
    out_handle.close()

elapsed = time.time() - start_time
speed = (success_count / elapsed) if elapsed > 0 else 0
print(f"\nShard {SHARD_ID} Complete! Labeled {success_count} items in {elapsed/60:.2f} mins ({speed:.2f} items/sec).")
print(f"Results saved to: {OUTPUT_FILE}")

# %% [code]
# 7. Quick Preview of Generated Labels for this Shard
print(f"\n--- Preview of First 3 Labeled Outputs (Shard {SHARD_ID}) ---")
preview_count = 0
with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        data = json.loads(line)
        print(f"\nItem ID: {data['id']}")
        print(f"Teacher JSON Label: {json.dumps(data.get('teacher_output', {}), indent=2)}")
        preview_count += 1
        if preview_count >= 3:
            break
