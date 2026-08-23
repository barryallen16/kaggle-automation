import os
from dotenv import load_dotenv
from fastapi import FastAPI
import subprocess
import csv
from pathlib import Path
import json
import io
from collections import defaultdict

kaggle_dir = Path.home() / ".kaggle"
ACCESS_TOKEN_FILEPATH = kaggle_dir / "access_token"

def csv_string_to_json(response):
    csv_string = io.StringIO(response.stdout)
    result = csv.DictReader(csv_string)
    return list(result)

def get_quota(key):
    with open(ACCESS_TOKEN_FILEPATH, "w" ) as f:
        f.write(key)
    results = subprocess.run(["kaggle", "quota", "-v"], capture_output=True, text=True, check=False)
    print(results)
    json_out = csv_string_to_json(results)
    print(json_out, type(json_out))
    return json_out

def get_usernames(key):
    with open(ACCESS_TOKEN_FILEPATH, "w") as f:
        f.write(key)
    nb_lst_str =subprocess.run(["kaggle", "kernels", "list", "-m", "-v"], capture_output=True, text=True, check=False)
    response = csv_string_to_json(nb_lst_str)
    if response:
        usrname = response[0]["ref"].split("/")[0]
    else:
        usrname = "unknown"
    return usrname

load_dotenv()
K_APIKEYS = {}
APIKEYS = [k.strip() for k in (os.getenv("KAGGLE_APIKEYS") or "").split(",") if k.strip()]
if not APIKEYS:
    raise SystemExit("KAGGLE_APIKEYS not set in environment or .env")
for key in APIKEYS:
    usrname = get_usernames(key)
    K_APIKEYS[usrname]=key
app = FastAPI()
quota_dict = defaultdict(list)
ALL_USRNAMES = list(K_APIKEYS.keys())
print(ALL_USRNAMES)

# @app.get("/")
# async def show_quota():
#     for usrname,key in K_APIKEYS.items():
#         quota_dict[usrname] = get_quota(key)
#     return quota_dict

# @app.get("/usrnames")
# async def get_usrs():
    # return k_APIKEYS.keys()
for usrname, key in K_APIKEYS.items():
    cur_acc_quota = get_quota(key)
    quota_dict[usrname] = cur_acc_quota
print(quota_dict)

