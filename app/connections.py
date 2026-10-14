"""Local connection profiles. Secrets are DPAPI-encrypted and never returned to the UI."""
import base64
import ctypes
import json
import os
import re
import threading
from urllib.parse import urlparse
from .config import DATA,legacy_settings,VERSION
from . import execution_budget

LOCK=threading.RLock()
PRESETS=[
    {"provider":"deepseek","label":"DeepSeek","base":"https://api.deepseek.com","model":"deepseek-chat","protocol":"openai"},
    {"provider":"openai","label":"OpenAI","base":"https://api.openai.com/v1","model":"","protocol":"openai"},
    {"provider":"qwen","label":"通义千问","base":"https://dashscope.aliyuncs.com/compatible-mode/v1","model":"qwen-plus","protocol":"openai"},
    {"provider":"anthropic","label":"Anthropic / Claude","base":"https://api.anthropic.com/v1","model":"","protocol":"anthropic"},
    {"provider":"ollama","label":"Ollama / 本地模型","base":"http://127.0.0.1:11434/v1","model":"","protocol":"openai"},
    {"provider":"custom","label":"自定义兼容接口","base":"","model":"","protocol":"openai"},
]
DATA_SOURCES=[
    {"id":"tushare","name":"Tushare Pro","description":"用户 Token；年度财务与证券目录","requires_key":True},
    {"id":"eastmoney","name":"东方财富公开财务","description":"无需密钥；与 AKShare 同源的公开财报接口，可能限流","requires_key":False},
    {"id":"documents","name":"仅使用披露文件","description":"选取已上传的年报，保留单一来源标记","requires_key":False},
]

def protect(secret,decrypt=False):
    if not secret: return ""
    if os.name!="nt": raise ValueError("当前安全密钥存储使用 Windows DPAPI；其他系统请使用环境变量")
    class Blob(ctypes.Structure):
        _fields_=[("cbData",ctypes.c_uint32),("pbData",ctypes.POINTER(ctypes.c_ubyte))]
    raw=base64.b64decode(secret) if decrypt else secret.encode("utf-8")
    buf=ctypes.create_string_buffer(raw)
    src=Blob(len(raw),ctypes.cast(buf,ctypes.POINTER(ctypes.c_ubyte)));dest=Blob()
    fn=ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not fn(ctypes.byref(src),None,None,None,None,1,ctypes.byref(dest)):
        raise ValueError("无法访问当前 Windows 用户的安全密钥存储")
    try: output=ctypes.string_at(dest.pbData,dest.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes=[ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(dest.pbData)
    return output.decode("utf-8") if decrypt else base64.b64encode(output).decode("ascii")

def validate_base(base):
    parsed=urlparse(base.strip())
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("API 地址必须为不含密钥、查询参数或账号的服务地址")
    if parsed.scheme!="https" and not (parsed.scheme=="http" and parsed.hostname in ("127.0.0.1","localhost","::1")):
        raise ValueError("远程 API 请使用 HTTPS；本机模型可使用 HTTP")
    return base.strip().rstrip("/")

def load():
    path=DATA/"connections.json"
    if path.exists():
        value=json.loads(path.read_text(encoding="utf-8"))
        value['execution_budget']=execution_budget.normalize(value.get('execution_budget'))
        return value
    legacy=legacy_settings()
    p=PRESETS[0]|{"id":"deepseek","name":"现有 DeepSeek","model":legacy["model"],"base":legacy["base"],"legacy_key":True,"json_mode":True}
    return {"active_model":"deepseek","data_source":"tushare","tushare_legacy":True,"profiles":[p],
            "execution_budget":execution_budget.normalize()}

def save(value):
    DATA.mkdir(parents=True,exist_ok=True)
    path=DATA/"connections.json";temp=path.with_suffix(".tmp")
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding="utf-8")
    temp.replace(path)

def resolve_model(profile_id=None):
    value=load();pid=profile_id or value["active_model"]
    profile=next((p for p in value["profiles"] if p["id"]==pid),None)
    if not profile: raise ValueError("模型连接不存在，请重新选择")
    key=legacy_settings()["key"] if profile.get("legacy_key") else protect(profile.get("secret",""),True)
    return profile|{"key":key}

def runtime_settings():
    value=load();profile=resolve_model()
    token=legacy_settings()["token"] if value.get("tushare_legacy",True) else protect(value.get("tushare_secret",""),True)
    return {**legacy_settings(),**profile,"token":token,"data_source":value.get("data_source","tushare"),"profile_id":profile["id"]}

def public_settings():
    from datetime import date
    value=load();cfg=runtime_settings()
    profiles=[{k:v for k,v in p.items() if k not in ("secret","legacy_key")}|{"key_configured":bool(legacy_settings()["key"] if p.get("legacy_key") else p.get("secret"))} for p in value["profiles"]]
    return {"version":VERSION,"today":date.today().isoformat(),"active_model":value["active_model"],"data_source":value["data_source"],
            "execution_budget":execution_budget.normalize(value.get('execution_budget')),
            "execution_budget_defaults":dict(execution_budget.DEFAULTS),
            "execution_budget_bounds":{key:dict(bounds) for key,bounds in execution_budget.BOUNDS.items()},
            "profiles":profiles,"presets":PRESETS,"data_sources":DATA_SOURCES,"tushare_configured":bool(cfg["token"]),
            "model_configured":bool(cfg["key"]) or cfg["provider"]=="ollama","model":cfg["model"],"model_host":urlparse(cfg["base"]).hostname,
            "secret_storage":"Windows 当前用户加密存储"}

def save_profile(data):
    with LOCK:
        value=load();pid=data["id"]
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,60}",pid): raise ValueError("连接标识无效")
        if data["provider"] not in {p["provider"] for p in PRESETS}: raise ValueError("不支持该模型厂商")
        base=validate_base(data["base"])
        old=next((p for p in value["profiles"] if p["id"]==pid),{})
        secret=data.get("api_key","").strip()
        if old and urlparse(old["base"]).netloc!=urlparse(base).netloc and not secret and not data.get("clear_key"):
            raise ValueError("服务地址已改变，请填写该服务的密钥或清除旧密钥，避免跨服务发送")
        p={k:data[k] for k in ("id","name","provider","model","protocol","json_mode")};p["base"]=base
        if secret: p.update(secret=protect(secret),legacy_key=False)
        elif data.get("clear_key"): p.update(secret="",legacy_key=False)
        else: p.update(secret=old.get("secret",""),legacy_key=old.get("legacy_key",False))
        value["profiles"]=[p if v["id"]==pid else v for v in value["profiles"]]
        if not old: value["profiles"].append(p)
        if data.get("activate"): value["active_model"]=pid
        save(value)
    return public_settings()

def save_preferences(data):
    with LOCK:
        value=load()
        if data.get('execution_budget') is not None:
            value['execution_budget']=execution_budget.normalize(data['execution_budget'])
        if data.get("active_model") is not None:
            if data["active_model"] not in {p["id"] for p in value["profiles"]}: raise ValueError("模型连接不存在")
            value["active_model"]=data["active_model"]
        if data.get("data_source") is not None:
            if data["data_source"] not in {p["id"] for p in DATA_SOURCES}: raise ValueError("数据源不存在")
            value["data_source"]=data["data_source"]
        if data.get("tushare_token"): value.update(tushare_secret=protect(data["tushare_token"].strip()),tushare_legacy=False)
        elif data.get("clear_tushare"): value.update(tushare_secret="",tushare_legacy=False)
        save(value)
    return public_settings()
