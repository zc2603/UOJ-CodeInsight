# UOJ CodeInsight

面向程序设计课程的代码理解测评平台。平台只读导入 UOJ Contest、题目和有效提交，在独立 PostgreSQL 中保存快照、预生成问题、学生作答与评分，不向 UOJ 写回数据。

## 测评协议

测评在数据库中保存协议版本。历史 `legacy` 测评继续使用顺序作答；`lightweight_v1` 支持按 Contest `problem_id` 升序固定原题顺序、逐原题配置第二问、自由切题和持久逐问草稿。三题及以上时，最大 `problem_id` 默认只生成一道 explanation 简答，其余原题默认各有一道简答和一道单选。学生只回答自己有有效提交的原题；教师无需再确认题序。

新协议一次交卷冻结全部最终答案，超时由持久评分队列收取已保存草稿。单选和空白题本地评分；非空简答调用版本化评分协议。教师可逐题人工复核并留下审计记录。成绩在结束、评分及复核完成后由教师主动公布；学生只读查看本人结果，并可逐题申诉。公布成绩统一包含参考答案与正确选项，已公布的历史结果也适用；未公布前不返回答案。教师成绩页提供统一宽度的概览、学生表格和申诉卡片，支持展开作答上下文、回复及逐题调整分数。

新建测评默认启用 `lightweight_v1`（`LIGHTWEIGHT_CREATION_ENABLED=true`）；独立质量审核仍暂停（`QUALITY_AUDIT_ENABLED=false`）。四个固定合成生成样本与六个固定合成评分样例已完成真实模型回归并经人工审阅。历史测评按记录中的协议版本继续运行；迁移 `0007_lightweight_v1` 不自动重生成历史题目或公布历史成绩。

## 开发验证

后端使用 Python 3.12、FastAPI、SQLAlchemy、Alembic 和 PostgreSQL；前端使用 React、TypeScript、Vite。Mock 测试不调用真实模型。

```sh
python -m venv .venv
# 安装 backend/requirements.txt 后
cd backend
python -m pytest -q
cd ../frontend
pnpm install --frozen-lockfile
pnpm run build
pnpm run test:statement
```

当前开发树后端离线回归为 193 项通过；前端构建及 6 项题面渲染检查通过。独立 PostgreSQL 16.15 合成数据库完成 `0006→0007` 升级和空库迁移，并验证草稿并写、统一交卷与超时、租约接管、公布竞态、申诉并发和合成数据清理。合成测试不代表生产容量。

早期三轮探索阶段共发出 14 次 HTTP，遇到的错误及费用估算保留在本地交接与回归记录。后续获批补齐评分前置条件后，固定十案例完整回归通过：四份生成结果和六个评分案例均符合预期并通过人工复核。最后五个评分样例发出 5 次 HTTP，usage 为 3388 输入 / 1519 输出 token，按官方 [DeepSeek API 高峰价](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/) 估算 ¥0.018928；该次授权总上限为 25 次 HTTP、并发 1、每样例最多 5 次、每次 100000 token、预算 ¥22。最大 `problem_id` 的第三题只生成一道 explanation；第四个修改题仅为不对应实际题号的辅助样本。生成 Prompt v2 明确区分认知类型 `type` 与作答形式 `response_format`，保留 v1/v2 任务兼容。合成回归执行器在每次 HTTP 发送前记录计数和费用预留；非 2xx 响应仅保存脱敏、限长的 `type`、`code`、`message`，不会记录错误正文。原始模型响应、题目文本、报告、claim 和内部验证材料仅保存在本地，不纳入仓库。

## 部署与数据边界

Compose 依次启动 PostgreSQL、迁移服务、后端与前端。涉及迁移时先备份数据库；前端离线镜像需包含本地构建的 `frontend/dist`。部署后核验健康、配置、迁移版本与资源哈希。维护脚本只是模板，安装副本须由管理员按环境审查。

仓库仅包含源码、Prompt、配置示例和合成测试。不得提交真实学生信息、代码、作答、成绩、数据库、运行日志、环境文件或密钥。真实模型回归须先批准固定样本、全部重试的硬请求上限、并发和预算。
