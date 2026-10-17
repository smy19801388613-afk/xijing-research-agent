# 第三方依赖、产品参考与复现

本文与 [dependency-inventory.json](dependency-inventory.json) 由 `scripts/inventory_dependencies.py` 根据锁文件及本地已安装包元数据生成。没有联网补全或推断许可证。JSON 保存完整版本、来源 URL、许可声明、许可文件相对路径、SHA-256、大小、开头文本摘要及缺失项。

## 仓库沿革与贡献

项目曾使用“研序 / yanxu-research-agent”名称，现使用“析鲸 / xijing-research-agent”。沿革与具体贡献以本仓库 Git 提交、PR 和文件历史为依据。名称变更本身不证明这是独立第三方代码的派生，也不构成对任何个人或团队版权归属的确认。参赛贡献说明应列明相应提交与实际实现范围。

本清单不为项目选择许可证。仓库自身授权由维护者另行明确；依赖包的声明不自动成为本项目许可证。

## 运行、构建与测试依赖

已锁定 Python 包 32 个；npm 包路径 49 个，当前安装 24 个。缺少的可选平台变体 25 个，仍列在完整清单中。

Python 直接依赖取自 `requirements.txt`，其中 pytest 用于测试；其余传递依赖逐项列出。前端运行/构建分类取自 npm 锁文件 dev 标记。许可列优先保留 SPDX 元数据，否则保留包声明或分类，不把“BSD License”等泛称擅自改成某个 SPDX 变体。

### Python

| 包 | 锁定版本 | 本地版本 | 层级 | 许可元数据 | 许可文件数 |
|---|---|---|---|---|---:|
| annotated-doc | 0.0.5 | 0.0.5 | 传递 | MIT | 1 |
| annotated-types | 0.8.0 | 0.8.0 | 传递 | MIT | 1 |
| anyio | 4.15.1 | 4.15.1 | 传递 | MIT | 1 |
| certifi | 2026.7.22 | 2026.7.22 | 传递 | MPL-2.0 | 1 |
| cffi | 2.1.1 | 2.1.1 | 传递 | MIT-0 | 1 |
| charset-normalizer | 3.5.1 | 3.5.1 | 传递 | MIT | 1 |
| click | 8.5.0 | 8.5.0 | 传递 | BSD-3-Clause | 1 |
| colorama | 0.4.6 | 0.4.6 | 传递 | SPDX 未提供；License :: OSI Approved :: BSD License | 1 |
| cryptography | 50.0.1 | 50.0.1 | 传递 | Apache-2.0 OR BSD-3-Clause | 3 |
| fastapi | 0.141.1 | 0.141.1 | 直接 | MIT | 1 |
| h11 | 0.16.0 | 0.16.0 | 传递 | MIT | 1 |
| httpcore | 1.0.9 | 1.0.9 | 传递 | BSD-3-Clause | 1 |
| httpx | 0.28.1 | 0.28.1 | 直接 | BSD-3-Clause | 1 |
| idna | 3.20 | 3.20 | 传递 | BSD-3-Clause | 1 |
| iniconfig | 2.3.0 | 2.3.0 | 传递 | MIT | 1 |
| packaging | 26.3 | 26.3 | 传递 | Apache-2.0 OR BSD-2-Clause | 3 |
| pdfminer.six | 20260107 | 20260107 | 传递 | MIT | 1 |
| pdfplumber | 0.11.10 | 0.11.10 | 直接 | SPDX 未提供；License :: OSI Approved :: MIT License | 1 |
| pillow | 12.3.0 | 12.3.0 | 传递 | MIT-CMU | 1 |
| pluggy | 1.6.0 | 1.6.0 | 传递 | MIT | 1 |
| pycparser | 3.0 | 3.0 | 传递 | BSD-3-Clause | 1 |
| pydantic | 2.13.5 | 2.13.5 | 传递 | MIT | 1 |
| pydantic_core | 2.46.5 | 2.46.5 | 传递 | MIT | 1 |
| Pygments | 2.21.0 | 2.21.0 | 传递 | BSD-2-Clause | 2 |
| pypdfium2 | 5.13.0 | 5.13.0 | 传递 | BSD-3-Clause, Apache-2.0, dependency licenses | 19 |
| pytest | 9.1.1 | 9.1.1 | 直接（测试） | MIT | 1 |
| python-dotenv | 1.2.3 | 1.2.3 | 直接 | BSD-3-Clause | 1 |
| python-multipart | 0.0.32 | 0.0.32 | 直接 | Apache-2.0 | 1 |
| starlette | 1.7.0 | 1.7.0 | 传递 | BSD-3-Clause | 1 |
| typing_extensions | 4.16.0 | 4.16.0 | 传递 | PSF-2.0 | 1 |
| typing-inspection | 0.4.4 | 0.4.4 | 传递 | MIT | 1 |
| uvicorn | 0.54.0 | 0.54.0 | 直接 | BSD-3-Clause | 1 |

### npm（含未安装的可选平台包）

| 包 / 路径 | 锁定版本 | 本地版本 | 用途 | 许可声明 | 许可文件数 |
|---|---|---|---|---|---:|
| @oxc-project/types | 0.151.0 | 0.151.0 | 构建 / 类型 / 工具 | MIT | 1 |
| @rolldown/binding-android-arm-eabi | 1.2.11 | 未安装 | 构建 / 类型 / 工具，可选 | MIT | 0 |
| @rolldown/binding-android-arm64 | 1.2.11 | 未安装 | 构建 / 类型 / 工具，可选 | MIT | 0 |
| @rolldown/binding-darwin-arm64 | 1.2.11 | 未安装 | 构建 / 类型 / 工具，可选 | MIT | 0 |
| @rolldown/binding-darwin-x64 | 1.2.11 | 未安装 | 构建 / 类型 / 工具，可选 | MIT | 0 |
| @rolldown/binding-freebsd-x64 | 1.2.11 | 未安装 | 构建 / 类型 / 工具，可选 | MIT | 0 |
| @rolldown/binding-linux-arm-gnueabihf | 1.2.11 | 未安装 | 构建 / 类型 / 工具，可选 | MIT | 0 |
| @rolldown/binding-linux-arm64-gnu | 1.2.11 | 未安装 | 构建 / 类型 / 工具，可选 | MIT | 0 |
| @rolldown/binding-linux-arm64-musl | 1.2.11 | 未安装 | 构建 / 类型 / 工具，可选 | MIT | 0 |
| @rolldown/binding-linux-ppc64-gnu | 1.2.11 | 未安装 | 构建 / 类型 / 工具，可选 | MIT | 0 |
| @rolldown/binding-linux-s390x-gnu | 1.2.11 | 未安装 | 构建 / 类型 / 工具，可选 | MIT | 0 |
| @rolldown/binding-linux-x64-gnu | 1.2.11 | 未安装 | 构建 / 类型 / 工具，可选 | MIT | 0 |
| @rolldown/binding-linux-x64-musl | 1.2.11 | 未安装 | 构建 / 类型 / 工具，可选 | MIT | 0 |
| @rolldown/binding-openharmony-arm64 | 1.2.11 | 未安装 | 构建 / 类型 / 工具，可选 | MIT | 0 |
| @rolldown/binding-win32-arm64-msvc | 1.2.11 | 未安装 | 构建 / 类型 / 工具，可选 | MIT | 0 |
| @rolldown/binding-win32-x64-msvc | 1.2.11 | 1.2.11 | 构建 / 类型 / 工具，可选 | MIT | 0 |
| @rolldown/pluginutils | 1.0.1 | 1.0.1 | 构建 / 类型 / 工具 | MIT | 1 |
| @types/react | 19.3.0 | 19.3.0 | 构建 / 类型 / 工具 | MIT | 1 |
| @types/react-dom | 19.3.0 | 19.3.0 | 构建 / 类型 / 工具 | MIT | 1 |
| @vitejs/plugin-react | 6.1.1 | 6.1.1 | 构建 / 类型 / 工具 | MIT | 1 |
| csstype | 3.2.3 | 3.2.3 | 构建 / 类型 / 工具 | MIT | 1 |
| detect-libc | 2.1.2 | 2.1.2 | 构建 / 类型 / 工具 | Apache-2.0 | 1 |
| fdir | 6.5.0 | 6.5.0 | 构建 / 类型 / 工具 | MIT | 1 |
| fsevents | 2.3.3 | 未安装 | 构建 / 类型 / 工具，可选 | MIT | 0 |
| lightningcss | 1.33.0 | 1.33.0 | 构建 / 类型 / 工具 | MPL-2.0 | 1 |
| lightningcss-android-arm64 | 1.33.0 | 未安装 | 构建 / 类型 / 工具，可选 | MPL-2.0 | 0 |
| lightningcss-darwin-arm64 | 1.33.0 | 未安装 | 构建 / 类型 / 工具，可选 | MPL-2.0 | 0 |
| lightningcss-darwin-x64 | 1.33.0 | 未安装 | 构建 / 类型 / 工具，可选 | MPL-2.0 | 0 |
| lightningcss-freebsd-x64 | 1.33.0 | 未安装 | 构建 / 类型 / 工具，可选 | MPL-2.0 | 0 |
| lightningcss-linux-arm-gnueabihf | 1.33.0 | 未安装 | 构建 / 类型 / 工具，可选 | MPL-2.0 | 0 |
| lightningcss-linux-arm64-gnu | 1.33.0 | 未安装 | 构建 / 类型 / 工具，可选 | MPL-2.0 | 0 |
| lightningcss-linux-arm64-musl | 1.33.0 | 未安装 | 构建 / 类型 / 工具，可选 | MPL-2.0 | 0 |
| lightningcss-linux-x64-gnu | 1.33.0 | 未安装 | 构建 / 类型 / 工具，可选 | MPL-2.0 | 0 |
| lightningcss-linux-x64-musl | 1.33.0 | 未安装 | 构建 / 类型 / 工具，可选 | MPL-2.0 | 0 |
| lightningcss-win32-arm64-msvc | 1.33.0 | 未安装 | 构建 / 类型 / 工具，可选 | MPL-2.0 | 0 |
| lightningcss-win32-x64-msvc | 1.33.0 | 1.33.0 | 构建 / 类型 / 工具，可选 | MPL-2.0 | 1 |
| lucide-react | 0.468.0 | 0.468.0 | 运行 | ISC | 1 |
| nanoid | 3.3.19 | 3.3.19 | 构建 / 类型 / 工具 | MIT | 1 |
| picocolors | 1.1.1 | 1.1.1 | 构建 / 类型 / 工具 | ISC | 1 |
| picomatch | 4.0.7 | 4.0.7 | 构建 / 类型 / 工具 | MIT | 1 |
| postcss | 8.5.28 | 8.5.28 | 构建 / 类型 / 工具 | MIT | 1 |
| react | 19.3.0 | 19.3.0 | 运行 | MIT | 1 |
| react-dom | 19.3.0 | 19.3.0 | 运行 | MIT | 1 |
| rolldown | 1.2.11 | 1.2.11 | 构建 / 类型 / 工具 | MIT | 2 |
| scheduler | 0.28.0 | 0.28.0 | 运行 | MIT | 1 |
| source-map-js | 1.2.1 | 1.2.1 | 构建 / 类型 / 工具 | BSD-3-Clause | 1 |
| tinyglobby | 0.2.17 | 0.2.17 | 构建 / 类型 / 工具 | MIT | 1 |
| typescript | 5.8.3 | 5.8.3 | 构建 / 类型 / 工具 | Apache-2.0 | 2 |
| vite | 8.3.1 | 8.3.1 | 构建 / 类型 / 工具 | MIT | 1 |

### 本地证据边界

- 版本不匹配时，锁定版本与本地版本分开列明；本地许可文件只能支持已安装版本。没有安装的可选平台包只有锁文件声明，不声称已核对其许可正文。
- pypdfium2 包含 PDFium 等原生组件的独立许可文件；这些文件的摘要与哈希一并保留。不能将整个包概括为单一 MIT 或忽略其内部依赖。Pillow、cryptography 等随包许可也按本地记录收集。
- 文件哈希用于检查内容一致性，不能证明授权充足、代码原创性或二进制供应链完整。需要分发许可正文时，应保留相应包中原始许可与 NOTICE 文件。
- 本次解释器 Python 3.12.4、内置 SQLite 3.45.3、Node v24.13.1。Python 安装目录的许可文件摘要在 JSON 的 runtime 中；Windows OCR 为可选系统组件，版本与许可不由 pip/npm 锁定。

当前环境差异：`{"python_version_mismatches": [], "python_not_installed": [], "npm_version_mismatches": [], "npm_absent_required": []}`。

## 产品设计参考与外部服务

| 对象 | 本项目使用方式 | 证据边界 |
|---|---|---|
| [Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) | 产品试用与研究纪律、反证机制设计参考 | 未列入 Python/npm 锁文件；不作为本项目运行时框架。旧版本读取用户本机 `.vibe-trading/.env` 的配置兼容逻辑，不等于导入其 Agent 代码。 |
| [FinSight](https://github.com/RUC-NLPIR/FinSight) | 问题拆解、检索和报告结构参考 | 未列入运行依赖；参考过程见 [设计审阅](reference-review-v0.3.md)。 |
| [FinRobot](https://github.com/AI4Finance-Foundation/FinRobot) | 可比估值与风险分析思路参考 | 未列入运行依赖；不宣称其开源模块在本系统中执行。 |
| 用户配置的大模型接口 | 通过 HTTP 协议调用推理服务 | 实际提供商、模型标识与调用消耗来自连接设置和任务日志，不能用本清单固定；不在仓库披露 API 密钥。 |
| Tushare、东方财富等数据接口 | 按用户配置获取证券资料与财务数据 | 接口数据、服务条款与开源软件许可证分别管理；来源和原始响应由任务留存。 |
| 巨潮、交易所、公司披露与用户上传文件 | 研究来源 | 保留原 URL / 页码 / 文件哈希；研究引用不改变原文权利。 |

上述产品参考分类依据当前锁文件、导入结构及既有设计记录，不代表已对 Git 全历史逐行完成复制代码审计。如后续实际引入第三方源码或模板，应单独登记原版本、文件、修改范围及许可，不应继续只写“思路参考”。

## 从固定源码复现

在独立目录取得准备提交的确切 Git commit，使用 Python 3.12 与 Node.js 22.12+（见 `scripts/setup.ps1`）；先安装锁定依赖，再构建。不要复制他人的 `.env`、连接配置或生产数据库。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/setup.ps1
.\.venv\Scripts\python.exe -X utf8 scripts/inventory_dependencies.py
.\.venv\Scripts\python.exe -X utf8 scripts/inventory_dependencies.py --check
.\.venv\Scripts\python.exe -X utf8 -m pytest -q
.\.venv\Scripts\python.exe -X utf8 scripts/evaluate_valuation_v050.py --output tmp/valuation-evaluation.json
```

`--check` 只比较清单，不修改文件；退出码 0 表示与当前锁文件、当前解释器和已安装包证据一致。不同操作系统可能安装不同可选包，导致清单不同；应保留对应环境新生成的清单并解释差异。脚本无时间戳，不读取密钥文件、不访问网络。

受控演示另建空数据目录，按赛事许可准备模型接口及指定 PDF / JSON / CSV，使用下列入口；材料导入、前置检查、财务分析、估值及备忘录沿用普通产品流程。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/start-controlled.ps1 -Workspace .\tmp\reproduction-controlled
```

具体访问策略见 [受控运行说明](competition/mode.md)。固定案例评测不使用模型，不能代替完整在线研究质量评测；模型输出还取决于供应商版本、参数与实际输入。执行证据包内的请求指纹不等于完整请求回放，正式演示应另存允许分享的材料、输入、原始执行记录与源码 commit。

锁文件确保版本选择；Python 当前锁文件不包含 wheel 内容哈希，npm integrity 也不证明运行环境或原生二进制相同。如需离线重放，应在许可允许范围内另行准备对应平台安装包、完整许可文件及 SHA-256 清单。
