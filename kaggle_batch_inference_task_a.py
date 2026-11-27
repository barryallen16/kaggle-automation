import json
import os
import sys
import time
from io import BytesIO

import requests
import torch
from PIL import Image
from tqdm.auto import tqdm

try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass


def _get_shard_var(name, default=None, cast=None):
    val = globals().get(name)
    if val is not None:
        return val
    env_val = os.environ.get(name)
    if env_val is not None and env_val.strip():
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

import subprocess


def run_cmd(cmd, description):
    print(f"{description}...")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(f"[ERROR] {description} failed (rc={result.returncode}):")
        out = (result.stdout or '')[-300:]
        err = (result.stderr or '')[-300:]
        if out: print(f"STDOUT: {out}")
        if err: print(f"STDERR: {err}")
        sys.exit(1)
    return result

USE_UV = subprocess.run(
    "uv --version", shell=True, capture_output=True, check=False
).returncode == 0

def install_cmd(args):
    return f"uv pip install --system {args}" if USE_UV else f"pip install -q {args}"

run_cmd(install_cmd("--no-deps accelerate bitsandbytes qwen-vl-utils"),
        "Installing inference deps without touching pre-installed torch")
run_cmd(f"{install_cmd('--no-deps')} git+https://github.com/huggingface/transformers.git",
        "Installing transformers from source (for Qwen3.5 MoE support)")
run_cmd(install_cmd("'tokenizers>=0.23.1' 'safetensors>=0.8.0' 'huggingface_hub>=0.30.0' 'requests>=2.32.0' 'regex' 'tqdm' 'pyyaml' 'filelock' 'fsspec' 'packaging' 'numpy' 'psutil' 'pillow' 'hf_transfer'"),
        "Installing transformers/accelerate runtime dependencies (excluding torch)")

try:
    import hf_transfer
    _ = hf_transfer
    os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"
    print("Fast model downloads enabled via hf_transfer")
except ImportError:
    print("hf_transfer unavailable - using standard HF downloads")

SCRATCH_DIR = "/kaggle/tmp"
if not os.path.exists(SCRATCH_DIR):
    SCRATCH_DIR = os.getcwd()

DATASET_URL = ("https://huggingface.co/datasets/barryallen16/"
               "fitcheck-annotate-dataset/resolve/main/task_a_dataset.jsonl")
DATASET_PATH = os.path.join(SCRATCH_DIR, "task_a_dataset.jsonl")
run_cmd(f"wget -q -O '{DATASET_PATH}' '{DATASET_URL}'",
        "Downloading Task A dataset from HuggingFace (to scratch, excluded from output)")

PRIOR_LABELED_URL = ("https://huggingface.co/datasets/barryallen16/"
                     "fitcheck-annotate-dataset/resolve/main/task_a_labeled.jsonl")
PRIOR_LABELED_PATH = os.path.join(SCRATCH_DIR, "task_a_labeled_prior.jsonl")
_prior = subprocess.run(f"wget -q -O '{PRIOR_LABELED_PATH}' '{PRIOR_LABELED_URL}'",
                        shell=True, capture_output=True, text=True, check=False)
if _prior.returncode == 0 and os.path.exists(PRIOR_LABELED_PATH):
    with open(PRIOR_LABELED_PATH, encoding="utf-8") as _f:
        _n_prior = sum(1 for l in _f if l.strip())
    print(f"Downloaded prior labels from HuggingFace ({_n_prior} items) - will skip them.")
else:
    print("No prior labels on the hub yet (404) - starting fresh.")

from qwen_vl_utils import process_vision_info
from transformers import AutoModelForImageTextToText, AutoProcessor, BitsAndBytesConfig

MODEL_ID = "Qwen/Qwen3.6-27B"

WORKING_DIR = "/kaggle/working" if os.path.exists("/kaggle/working") else os.getcwd()

INPUT_FILE = DATASET_PATH if os.path.exists(DATASET_PATH) else None
if not INPUT_FILE:
    _cwd_candidate = os.path.join(os.getcwd(), "task_a_dataset.jsonl")
    if os.path.exists(_cwd_candidate):
        INPUT_FILE = _cwd_candidate
if not INPUT_FILE:
    for root, _, files in os.walk("/kaggle/input"):
        for file in files:
            if file.endswith("task_a_dataset.jsonl"):
                INPUT_FILE = os.path.join(root, file)
                break
        if INPUT_FILE:
            break

if TOTAL_SHARDS > 1:
    OUTPUT_FILE = os.path.join(WORKING_DIR, f"task_a_labeled_shard_{SHARD_ID}.jsonl")
else:
    OUTPUT_FILE = os.path.join(WORKING_DIR, "task_a_labeled.jsonl")

CHECKPOINT_INTERVAL = 25

MAX_NEW_TOKENS = 384

MAX_ITEMS_PER_RUN = int(os.environ.get("MAX_ITEMS_PER_RUN", "0") or 0)
MAX_RUNTIME_MINUTES = _get_shard_var("MAX_RUNTIME_MINUTES", 0.0, cast=float) or 0.0

print(f"Input file path:  {INPUT_FILE}")
print(f"Output file path: {OUTPUT_FILE}")
print(f"Active GPU count: {torch.cuda.device_count()}")
for i in range(torch.cuda.device_count()):
    print(f"  GPU {i}: {torch.cuda.get_device_name(i)} ({torch.cuda.get_device_properties(i).total_memory / (1024**3):.1f} GB)")

print(f"\nLoading {MODEL_ID} with 4-bit quantization across dual T4 GPUs...")

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

gpu_mem_budget = {i: f"{int(torch.cuda.get_device_properties(i).total_memory / (1024**3)) - 1}.5GiB" for i in range(gpu_count)}
print(f"Memory budget: {gpu_mem_budget}")

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

model = AutoModelForImageTextToText.from_pretrained(
    MODEL_ID,
    quantization_config=bnb_config,
    device_map="auto",
    max_memory=gpu_mem_budget,
    trust_remote_code=True
)

processor = AutoProcessor.from_pretrained(
    MODEL_ID,
    trust_remote_code=True,
    min_pixels=256 * 28 * 28,
    max_pixels=768 * 28 * 28
)
print("Model and processor successfully loaded across GPUs!")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def fetch_image(url: str, timeout: int = 8, retries: int = 2) -> Image.Image:
    img = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            if resp.status_code == 200:
                img = Image.open(BytesIO(resp.content)).convert("RGB")
                max_dim = 768
                if max(img.size) > max_dim:
                    img.thumbnail((max_dim, max_dim))
                return img
        except Exception:
            pass
        if attempt < retries - 1:
            time.sleep(1)
    return img

def extract_json_response(raw_text: str) -> dict:
    raw_text = raw_text.strip()
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

all_items = []
with open(INPUT_FILE, "r", encoding="utf-8") as f:
    for line in f:
        if line.strip():
            all_items.append(json.loads(line))

total_items = len(all_items)
print(f"Total dataset items found: {total_items}")

if START_INDEX is not None and END_INDEX is not None:
    shard_items = all_items[START_INDEX:END_INDEX]
elif TOTAL_SHARDS > 1:
    items_per_shard = (total_items + TOTAL_SHARDS - 1) // TOTAL_SHARDS
    s_idx = SHARD_ID * items_per_shard
    e_idx = min(s_idx + items_per_shard, total_items)
    shard_items = all_items[s_idx:e_idx]
    print(f"Computed slice for Shard {SHARD_ID}: {s_idx} to {e_idx}")
else:
    shard_items = all_items

print(f"Total items assigned to this shard: {len(shard_items)}")

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

_prior_path = PRIOR_LABELED_PATH
if not os.path.exists(_prior_path):
    _prior_path = os.path.join(os.getcwd(), "task_a_labeled_prior.jsonl")
if os.path.exists(_prior_path):
    with open(_prior_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                data = json.loads(line)
                if data.get("id"):
                    processed_ids.add(data["id"])
            except Exception:
                pass
    print(f"Total unique skip-set after merging prior hub labels: {len(processed_ids)}")

items_to_process = [item for item in shard_items if item.get("id") not in processed_ids]
print(f"Remaining items to process on this shard: {len(items_to_process)}")

success_count = 0
failed_count = 0
completion_lengths = []

start_time = time.time()

with open(OUTPUT_FILE, "a", encoding="utf-8") as out_handle:
    try:
        total_on_shard = len(items_to_process)
        if MAX_ITEMS_PER_RUN:
            print(f"[LIMIT] This session will stop after {MAX_ITEMS_PER_RUN} items (timing/batch mode).", flush=True)
        if MAX_RUNTIME_MINUTES > 0:
            print(f"[LIMIT] This session will stop after {MAX_RUNTIME_MINUTES:g} minutes (quota cap).", flush=True)
        for idx, item in enumerate(tqdm(items_to_process, desc=f"Shard {SHARD_ID} [{MODEL_ID}]"), start=1):
            if MAX_ITEMS_PER_RUN and idx > MAX_ITEMS_PER_RUN:
                print(f"[LIMIT] Reached MAX_ITEMS_PER_RUN={MAX_ITEMS_PER_RUN} - stopping session cleanly.", flush=True)
                break
            if MAX_RUNTIME_MINUTES > 0 and (time.time() - start_time) >= MAX_RUNTIME_MINUTES * 60:
                print(f"[LIMIT] Reached MAX_RUNTIME_MINUTES={MAX_RUNTIME_MINUTES:g} - stopping session cleanly.", flush=True)
                break
            tok_stat = ""
            if completion_lengths:
                avg_tok = sum(completion_lengths) / len(completion_lengths)
                tok_stat = f" | tok avg={avg_tok:.0f} max={max(completion_lengths)}"
            print(
                f"[PROGRESS {idx}/{total_on_shard}] starting id={item.get('id')} "
                f"| ok={success_count} fail={failed_count}{tok_stat} "
                f"| {time.strftime('%H:%M:%S')}",
                flush=True
            )
            item_id = item["id"]
            img_urls = item.get("images", [])
            prompt_text = item.get("prompt", "")

            pil_images = []
            for url in img_urls[:4]:
                img = fetch_image(url)
                if img is not None:
                    pil_images.append(img)

            if not pil_images:
                print(f"\n[Warning] Could not load images for {item_id}, skipping.")
                failed_count += 1
                continue

            content = []
            for img in pil_images:
                content.append({"type": "image", "image": img})
            content.append({"type": "text", "text": prompt_text})

            messages = [{"role": "user", "content": content}]

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

            with torch.no_grad():
                generated_ids = model.generate(
                    **inputs,
                    max_new_tokens=MAX_NEW_TOKENS,
                    do_sample=False
                )

            generated_ids_trimmed = [
                out_ids[len(in_ids):] for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
            ]
            completion_tokens = len(generated_ids_trimmed[0])
            completion_lengths.append(completion_tokens)
            response_text = processor.batch_decode(
                generated_ids_trimmed,
                skip_special_tokens=True,
                clean_up_tokenization_spaces=False
            )[0]

            parsed_json = extract_json_response(response_text)

            parse_failed = set(parsed_json.keys()) == {"raw_output"}
            if parse_failed or completion_tokens >= MAX_NEW_TOKENS:
                failed_count += 1
                print(
                    f"[SKIP] id={item_id}: "
                    + ("unparseable output" if parse_failed else "truncated at cap")
                    + f" ({completion_tokens} tokens) - will retry on next run",
                    flush=True
                )
                continue

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

elapsed = time.time() - start_time
speed = (success_count / elapsed) if elapsed > 0 else 0
print(f"\nShard {SHARD_ID} Complete! Labeled {success_count} items in {elapsed/60:.2f} mins ({speed:.2f} items/sec).")
print(f"Results saved to: {OUTPUT_FILE}")

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
