# 通用研究与受控运行

v0.4.9 起，普通研究与受控运行共用资料库、新研究、执行记录和证据包。产品主导航不再提供独立比赛页面。官方通知要求受控数据环境、过程记录和可复现交付，并未指定必须有名为“比赛模式”的页面。

## 日常使用

在资料库上传 PDF 或 JSON/CSV，填写证券、年度和公告日期。结构化材料可以查看原始数据，再用于新研究；新研究中也可勾选同公司、同年度的材料。选择“指定研究材料”时不调用财务数据接口。材料与模型配置在启动前检查，不发起付费模型调用；已配置不等于连通性、余额或生成质量已验证。

JSON 为行数组或 `{"rows":[...]}`，CSV 为同名字段：`metric、year、value、unit、currency、scope、period_type、announced_date`。目前支持 `revenue、n_income_attr_p、n_cashflow_act` 三项年度核心指标，单位元/千元/万元，币种 CNY、口径 consolidated、期间 annual。每行公告日期不晚于截止日；也可在上传时填写统一公告日期。缺日期的行保留缺口，未知指标不并入财务计算。导入材料始终按单源处理，不自动升级为独立双源核验。

[示例 CSV](../../examples/competition-financials.csv) 是合成数字，只用于流程测试，不能当作真实公司财务。新文件写入 `structured-materials/`，旧 `competition-materials/` 原地兼容读取，不迁移或改写。

## 高级访问配置

连接设置中折叠的“高级：运行与数据访问”显示实际运行策略。普通研究不会因使用材料或执行日志而进入受控运行。需要独立目录和网络许可时，先停止当前服务，然后使用：

```powershell
powershell -File scripts/start-controlled.ps1 -Workspace .\var-controlled -Policy .\examples\competition-policy.json
```

策略在进程启动时冻结并传给后台执行进程。必须使用独立数据目录，不能复用普通 `var`。策略分开设置 `model_origins` 与 `retrieval_origins`，按协议、域名和端口精确匹配。示例仅许可 DeepSeek 模型接口且不许可资料联网；这是可替换的配置，不是官方指定模型。连接和密钥仍通过连接设置配置。

停止指定受控工作区：`powershell -File scripts/stop.ps1 -Workspace .\var-controlled`。修改策略或目录必须重启。应用层目录/网络限制不替代运行机器的隔离；现场允许的网络范围以赛事安排为准。

原 `start-competition.ps1`、`RESEARCH_COMPETITION_*` 环境变量、`/api/competition/*` 和 `/submission` 接口继续兼容。CLI `scripts/run_competition.py` 仍可执行固定材料研究；默认不调用模型，加 `--model` 才使用已配置模型。

## 执行记录与导出

普通与受控运行都把新事件记录到 `execution-audit.sqlite3`：模型调用、工具参数/结果、文件指纹、研究快照等。旧 `competition-audit.sqlite3` 只读兼容；此前未记录的步骤不会补造。

公司研究页可按需查看当前研究的详细执行记录。导出“完整证据包”包含当前报告、历史研究底稿、所选 PDF/JSON/CSV、材料预检、计算、当前研究关联的执行记录、依赖版本和源文件 SHA-256。全工作区日志、未关联材料、连接配置、密钥文件及 worker 日志不会打包。

事件按研究 ID 关联。尚未绑定研究的准备事件只按明确归属的对话轮次关联，不把同一会话的全部历史带入导出。模型请求可能包含跨轮上下文，页面和证据包仅展示请求指纹与大小；完整脱敏请求保留在本地审计库。这意味着证据包不包含完整提示词回放，完整源码仍须另行提供，真实模型重跑也不保证逐字一致。

证据包的新文件位于 `execution/`，旧 `/submission` 额外保留 `competition/` 路径别名，两者内容具有相同的当前研究范围。
