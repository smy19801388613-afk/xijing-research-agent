"""Deterministic offline inventory of locked packages and local license evidence.

Reads only lock/manifests, installed distribution metadata, package.json files,
and named license/notice files. It never reads .env, connection files, user data,
registry credentials, or remote package metadata. Run using the project's venv.
"""
import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
from urllib.parse import urlsplit,urlunsplit

ROOT=Path(__file__).resolve().parents[1]
LICENSE_NAME=re.compile(r'license|licence|copying|copyright|notice',re.I)


def digest(data): return hashlib.sha256(data).hexdigest()
def normalized(name): return re.sub(r'[-_.]+','-',name).lower()
def read_json(path): return json.loads(path.read_text(encoding='utf-8'))


def public_url(value):
    if not isinstance(value,str): return None
    value=value.removeprefix('git+')
    try:
        parsed=urlsplit(value)
        if parsed.scheme not in ('http','https') or not parsed.hostname:return None
        # Package URLs do not need auth/query strings in a shareable inventory.
        host=parsed.hostname+(':'+str(parsed.port) if parsed.port else '')
        return urlunsplit((parsed.scheme,host,parsed.path,'',''))
    except ValueError:return None


def license_file(path,relative):
    if not path.is_file():return None
    # A package may expose a Python module named "licenses". Its source and
    # path-dependent bytecode are executable code, not bundled license text.
    if path.suffix.lower() in ('.py','.pyc','.pyo','.pyd','.dll','.so','.exe','.wasm','.node'):return None
    data=path.read_bytes();text=data.decode('utf-8',errors='replace')
    leading=' / '.join(line.strip() for line in text.splitlines() if line.strip())[:260]
    return {'path':relative.replace('\\','/'),'sha256':digest(data),'bytes':len(data),
        'leading_text_excerpt':leading,'spdx_identifiers':sorted(set(re.findall(r'SPDX-License-Identifier:\s*([^\r\n]+)',text)))}


def pins(path):
    result=[]
    for line in path.read_text(encoding='utf-8').splitlines():
        line=line.strip()
        if not line or line.startswith('#'):continue
        found=re.fullmatch(r'([A-Za-z0-9_.-]+)==([^\s;]+)',line)
        if not found:raise ValueError(f'Unsupported non-exact pin in {path.name}: {line[:120]}')
        result.append(found.groups())
    return result


def python_inventory(root):
    direct={normalized(name) for name,_ in pins(root/'requirements.txt')}
    result=[]
    for name,version in sorted(pins(root/'requirements-lock.txt'),key=lambda item:normalized(item[0])):
        row={'name':name,'locked_version':version,'direct':normalized(name) in direct,
            'metadata_source':'local installed distribution','installed_version':None,
            'installed_matches_lock':False,'license_expression':None,'license_declared':None,
            'license_classifiers':[],'project_urls':{},'license_files':[],'requires_dist':[]}
        try:dist=metadata.distribution(name)
        except metadata.PackageNotFoundError:
            row['status']='not_installed';row['limitations']=['No installed metadata: license and project source are unknown.']
            result.append(row);continue
        meta=dist.metadata;row['installed_version']=dist.version;row['installed_matches_lock']=dist.version==version
        row['status']='matched' if row['installed_matches_lock'] else 'version_mismatch'
        row['license_expression']=meta.get('License-Expression') or None
        declared=meta.get('License')
        if declared:
            row['license_declared']=' '.join(declared.split())[:300]
            row['license_declared_sha256']=digest(declared.encode())
            row['license_declared_truncated']=len(' '.join(declared.split()))>300
        row['license_classifiers']=sorted(value for value in meta.get_all('Classifier',[]) if value.startswith('License ::'))
        for entry in meta.get_all('Project-URL',[]):
            label,sep,url=entry.partition(',')
            if sep and public_url(url.strip()):row['project_urls'][label.strip()]=public_url(url.strip())
        if public_url(meta.get('Home-page')):row['project_urls'].setdefault('Homepage',public_url(meta['Home-page']))
        row['requires_dist']=sorted(meta.get_all('Requires-Dist',[]))
        base=Path(dist.locate_file('')).resolve()
        for file in sorted(dist.files or [],key=str):
            if not any(LICENSE_NAME.search(part) for part in file.parts):continue
            path=Path(dist.locate_file(file)).resolve()
            if not path.is_relative_to(base):continue
            found=license_file(path,str(file))
            if found:row['license_files'].append(found)
        row['limitations']=[]
        if not row['license_expression']:row['limitations'].append('No SPDX License-Expression metadata; declared license/classifiers and bundled texts are retained without inference.')
        if not row['license_files']:row['limitations'].append('No bundled license/notice file found in distribution file records.')
        if not row['project_urls']:row['limitations'].append('Project source URL not provided by local metadata.')
        if not row['installed_matches_lock']:row['limitations'].append('License evidence belongs to installed_version, not an uninspected locked version.')
        result.append(row)
    return result


def npm_license_files(path):
    found=[]
    if not path.is_dir():return found
    for file in sorted(path.iterdir(),key=lambda p:p.name.lower()):
        if not LICENSE_NAME.search(file.name):continue
        candidates=sorted(file.rglob('*')) if file.is_dir() else [file]
        for candidate in candidates:
            if not candidate.resolve().is_relative_to(path.resolve()):continue
            item=license_file(candidate,candidate.relative_to(path).as_posix())
            if item:found.append(item)
    return found


def npm_inventory(root):
    web=root/'web';lock=read_json(web/'package-lock.json');manifest=read_json(web/'package.json')
    direct=set(manifest.get('dependencies',{}))|set(manifest.get('devDependencies',{}))
    rows=[]
    for key,item in sorted(lock['packages'].items()):
        if not key:continue
        name=key.rsplit('node_modules/',1)[-1];package=web/key
        record={'name':name,'lock_path':key,'locked_version':item.get('version'),
            'direct':name in direct and key=='node_modules/'+name,
            'role':'build_or_test' if item.get('dev') else 'runtime',
            'optional':bool(item.get('optional')),'os':item.get('os',[]),'cpu':item.get('cpu',[]),
            'lock_license':item.get('license'),'registry_tarball':public_url(item.get('resolved')),
            'integrity':item.get('integrity'),'installed_version':None,'installed_matches_lock':False,
            'installed_license':None,'project_urls':{},'license_files':[],'limitations':[]}
        if (package/'package.json').is_file() and package.resolve().is_relative_to((web/'node_modules').resolve()):
            installed=read_json(package/'package.json');record['installed_version']=installed.get('version')
            record['installed_matches_lock']=record['installed_version']==record['locked_version']
            record['status']='matched' if record['installed_matches_lock'] else 'version_mismatch'
            record['installed_license']=installed.get('license')
            repo=installed.get('repository');repo=repo.get('url') if isinstance(repo,dict) else repo
            for label,value in (('Repository',repo),('Homepage',installed.get('homepage'))):
                if public_url(value):record['project_urls'][label]=public_url(value)
            record['license_files']=npm_license_files(package)
            if not record['installed_matches_lock']:record['limitations'].append('Local package metadata/license files apply to installed_version only.')
        else:
            record['status']='not_installed'
            record['limitations'].append('Local package absent; lock metadata only. Optional OS/CPU variants may intentionally be absent.')
        if not record['lock_license'] and not record['installed_license']:record['limitations'].append('License declaration unknown: neither inspected lock nor installed package provides it.')
        if not record['license_files']:record['limitations'].append('No local license/notice text inspected for this package.')
        if not record['project_urls']:record['limitations'].append('Project repository URL unknown; registry tarball is a distribution source, not a repository assertion.')
        rows.append(record)
    return rows


def command_version(command):
    path=shutil.which(command)
    if not path:return None
    try:return subprocess.run([path,'--version'],capture_output=True,text=True,check=True,timeout=10).stdout.strip()
    except (OSError,subprocess.SubprocessError):return None


def generate(root):
    python=python_inventory(root);npm=npm_inventory(root)
    interpreter_files=[]
    for file in sorted(Path(sys.base_prefix).iterdir()):
        if file.is_file() and LICENSE_NAME.search(file.name):
            interpreter_files.append(license_file(file,file.name))
    inputs={path:digest((root/path).read_bytes()) for path in ('requirements.txt','requirements-lock.txt','web/package.json','web/package-lock.json')}
    return {'schema_version':1,'generator':'scripts/inventory_dependencies.py',
        'scope':'Offline package inventory; metadata declarations and local file evidence, not a legal determination or full binary SBOM.',
        'determinism':'No timestamp, absolute user paths, credentials, environment dump or network access. Same inputs and installed metadata produce identical files.',
        'input_sha256':inputs,'runtime':{'python':sys.version.split()[0],'sqlite_bundled_version':sqlite3.sqlite_version,
            'node':command_version('node'),'python_license_files':interpreter_files,
            'windows_ocr':'Optional Windows.Media.Ocr system component; not a pip/npm package; OS-specific license not inspected.'},
        'summary':{'python_locked':len(python),'python_version_mismatches':[p['name'] for p in python if p['status']=='version_mismatch'],
            'python_not_installed':[p['name'] for p in python if p['status']=='not_installed'],
            'npm_locked':len(npm),'npm_installed':sum(p['installed_version'] is not None for p in npm),
            'npm_version_mismatches':[p['lock_path'] for p in npm if p['status']=='version_mismatch'],
            'npm_absent_optional':sum(p['status']=='not_installed' and p['optional'] for p in npm),
            'npm_absent_required':[p['lock_path'] for p in npm if p['status']=='not_installed' and not p['optional']]},
        'python_packages':python,'npm_packages':npm}


def cell(value):return str(value if value is not None else '未知').replace('|',' / ').replace('\n',' ')
def license_label(row):
    value=row.get('license_expression') or row.get('license_declared') or row.get('lock_license') or row.get('installed_license')
    if value:return cell(value)[:120]
    classifiers=row.get('license_classifiers')
    return 'SPDX 未提供；'+cell(', '.join(classifiers)) if classifiers else '未知（无声明）'


def render_markdown(data):
    counts=data['summary'];runtime=data['runtime']
    lines=['# 第三方依赖、产品参考与复现', '',
        '本文与 [dependency-inventory.json](dependency-inventory.json) 由 `scripts/inventory_dependencies.py` 根据锁文件及本地已安装包元数据生成。没有联网补全或推断许可证。JSON 保存完整版本、来源 URL、许可声明、许可文件相对路径、SHA-256、大小、开头文本摘要及缺失项。', '',
        '## 仓库沿革与贡献', '',
        '项目曾使用“研序 / yanxu-research-agent”名称，现使用“析鲸 / xijing-research-agent”。沿革与具体贡献以本仓库 Git 提交、PR 和文件历史为依据。名称变更本身不证明这是独立第三方代码的派生，也不构成对任何个人或团队版权归属的确认。参赛贡献说明应列明相应提交与实际实现范围。', '',
        '本清单不为项目选择许可证。仓库自身授权由维护者另行明确；依赖包的声明不自动成为本项目许可证。', '',
        '## 运行、构建与测试依赖', '',
        f"已锁定 Python 包 {counts['python_locked']} 个；npm 包路径 {counts['npm_locked']} 个，当前安装 {counts['npm_installed']} 个。缺少的可选平台变体 {counts['npm_absent_optional']} 个，仍列在完整清单中。", '',
        'Python 直接依赖取自 `requirements.txt`，其中 pytest 用于测试；其余传递依赖逐项列出。前端运行/构建分类取自 npm 锁文件 dev 标记。许可列优先保留 SPDX 元数据，否则保留包声明或分类，不把“BSD License”等泛称擅自改成某个 SPDX 变体。', '',
        '### Python', '', '| 包 | 锁定版本 | 本地版本 | 层级 | 许可元数据 | 许可文件数 |','|---|---|---|---|---|---:|']
    for p in data['python_packages']:
        lines.append(f"| {p['name']} | {p['locked_version']} | {p['installed_version'] or '未安装'} | {'直接（测试）' if normalized(p['name'])=='pytest' else '直接' if p['direct'] else '传递'} | {license_label(p)} | {len(p['license_files'])} |")
    lines+=['','### npm（含未安装的可选平台包）','','| 包 / 路径 | 锁定版本 | 本地版本 | 用途 | 许可声明 | 许可文件数 |','|---|---|---|---|---|---:|']
    for p in data['npm_packages']:
        lines.append(f"| {p['name']} | {p['locked_version']} | {p['installed_version'] or '未安装'} | {'运行' if p['role']=='runtime' else '构建 / 类型 / 工具'}{'，可选' if p['optional'] else ''} | {license_label(p)} | {len(p['license_files'])} |")
    lines+=['','### 本地证据边界','',
        '- 版本不匹配时，锁定版本与本地版本分开列明；本地许可文件只能支持已安装版本。没有安装的可选平台包只有锁文件声明，不声称已核对其许可正文。',
        '- pypdfium2 包含 PDFium 等原生组件的独立许可文件；这些文件的摘要与哈希一并保留。不能将整个包概括为单一 MIT 或忽略其内部依赖。Pillow、cryptography 等随包许可也按本地记录收集。',
        '- 文件哈希用于检查内容一致性，不能证明授权充足、代码原创性或二进制供应链完整。需要分发许可正文时，应保留相应包中原始许可与 NOTICE 文件。',
        f"- 本次解释器 Python {runtime['python']}、内置 SQLite {runtime['sqlite_bundled_version']}、Node {runtime['node'] or '未知'}。Python 安装目录的许可文件摘要在 JSON 的 runtime 中；Windows OCR 为可选系统组件，版本与许可不由 pip/npm 锁定。", '',
        '当前环境差异：`'+json.dumps({k:v for k,v in counts.items() if k.endswith('mismatches') or k.endswith('required') or k=='python_not_installed'},ensure_ascii=False)+'`。', '',
        '## 产品设计参考与外部服务', '',
        '| 对象 | 本项目使用方式 | 证据边界 |','|---|---|---|',
        '| [Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) | 产品试用与研究纪律、反证机制设计参考 | 未列入 Python/npm 锁文件；不作为本项目运行时框架。旧版本读取用户本机 `.vibe-trading/.env` 的配置兼容逻辑，不等于导入其 Agent 代码。 |',
        '| [FinSight](https://github.com/RUC-NLPIR/FinSight) | 问题拆解、检索和报告结构参考 | 未列入运行依赖；参考过程见 [设计审阅](reference-review-v0.3.md)。 |',
        '| [FinRobot](https://github.com/AI4Finance-Foundation/FinRobot) | 可比估值与风险分析思路参考 | 未列入运行依赖；不宣称其开源模块在本系统中执行。 |',
        '| 用户配置的大模型接口 | 通过 HTTP 协议调用推理服务 | 实际提供商、模型标识与调用消耗来自连接设置和任务日志，不能用本清单固定；不在仓库披露 API 密钥。 |',
        '| Tushare、东方财富等数据接口 | 按用户配置获取证券资料与财务数据 | 接口数据、服务条款与开源软件许可证分别管理；来源和原始响应由任务留存。 |',
        '| 巨潮、交易所、公司披露与用户上传文件 | 研究来源 | 保留原 URL / 页码 / 文件哈希；研究引用不改变原文权利。 |', '',
        '上述产品参考分类依据当前锁文件、导入结构及既有设计记录，不代表已对 Git 全历史逐行完成复制代码审计。如后续实际引入第三方源码或模板，应单独登记原版本、文件、修改范围及许可，不应继续只写“思路参考”。', '',
        '## 从固定源码复现', '',
        '在独立目录取得准备提交的确切 Git commit，使用 Python 3.12 与 Node.js 22.12+（见 `scripts/setup.ps1`）；先安装锁定依赖，再构建。不要复制他人的 `.env`、连接配置或生产数据库。', '',
        '```powershell',
        'powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/setup.ps1',
        r'.\.venv\Scripts\python.exe -X utf8 scripts/inventory_dependencies.py',
        r'.\.venv\Scripts\python.exe -X utf8 scripts/inventory_dependencies.py --check',
        r'.\.venv\Scripts\python.exe -X utf8 -m pytest -q',
        r'.\.venv\Scripts\python.exe -X utf8 scripts/evaluate_valuation_v050.py --output tmp/valuation-evaluation.json',
        '```','',
        '`--check` 只比较清单，不修改文件；退出码 0 表示与当前锁文件、当前解释器和已安装包证据一致。不同操作系统可能安装不同可选包，导致清单不同；应保留对应环境新生成的清单并解释差异。脚本无时间戳，不读取密钥文件、不访问网络。', '',
        '受控演示另建空数据目录，按赛事许可准备模型接口及指定 PDF / JSON / CSV，使用下列入口；材料导入、前置检查、财务分析、估值及备忘录沿用普通产品流程。', '',
        '```powershell',
        r'powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/start-controlled.ps1 -Workspace .\tmp\reproduction-controlled',
        '```','',
        '具体访问策略见 [受控运行说明](competition/mode.md)。固定案例评测不使用模型，不能代替完整在线研究质量评测；模型输出还取决于供应商版本、参数与实际输入。执行证据包内的请求指纹不等于完整请求回放，正式演示应另存允许分享的材料、输入、原始执行记录与源码 commit。', '',
        '锁文件确保版本选择；Python 当前锁文件不包含 wheel 内容哈希，npm integrity 也不证明运行环境或原生二进制相同。如需离线重放，应在许可允许范围内另行准备对应平台安装包、完整许可文件及 SHA-256 清单。', '']
    return '\n'.join(lines)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--check',action='store_true')
    args=parser.parse_args();data=generate(ROOT)
    outputs={ROOT/'docs/dependency-inventory.json':json.dumps(data,ensure_ascii=False,indent=2,sort_keys=True)+'\n',
             ROOT/'docs/third-party.md':render_markdown(data)}
    mismatches=[]
    for path,content in outputs.items():
        if args.check:
            if not path.is_file() or path.read_text(encoding='utf-8')!=content:mismatches.append(path.relative_to(ROOT).as_posix())
        else:path.write_text(content,encoding='utf-8',newline='\n')
    print(json.dumps({'check':args.check,'matches':not mismatches,'changed_or_missing':mismatches,'summary':data['summary']},ensure_ascii=False))
    return 1 if mismatches else 0


if __name__=='__main__':raise SystemExit(main())
