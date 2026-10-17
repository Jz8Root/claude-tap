# 脚本

[English](README.md)

## `check_coverage.py`

检查后端 Python 代码及 `claude_tap/viewer.html` 内嵌 JavaScript、CSS 的项目和增量覆盖率。

目标在 `pyproject.toml` 的 `[tool.claude_tap.coverage]` 中配置：

- Python 项目覆盖率：`python_total_min`
- Python 包内变更的可执行行覆盖率：`python_diff_min`
- 查看器 JavaScript 函数覆盖率：`viewer_js_function_min`
- 查看器变更的 JavaScript 函数覆盖率：`viewer_js_diff_min`
- 查看器 CSS 选择器覆盖率：`viewer_css_selector_min`
- 查看器变更的 CSS 选择器覆盖率：`viewer_css_diff_min`

### 用法

```bash
python -m coverage run -m pytest tests/ -q
python -m coverage json -o .coverage.json
python scripts/check_coverage.py --python-coverage .coverage.json
```

## `check_pr_policy.py`

根据 PR 修改的文件检查正文策略，要求：

- 包含 Summary/Problem/Goal 和 Validation/Test plan/Results 章节。
- 运行时、查看器、客户端、代理或 UI 行为变更包含 `raw.githubusercontent.com` 截图证据。
- PR 图片链接使用 `raw.githubusercontent.com`。
- PR 不包含原始 trace、生成的 trace 查看器、日志或疑似包含密钥的文件。
- PR 正文不包含明显的 API 密钥或 bearer token。

### 用法

```bash
python scripts/check_pr_policy.py \
  --body-file /tmp/pr-body.md \
  --changed-files-file /tmp/pr-files.txt
```

在 GitHub Actions 中传入事件数据：

```bash
python scripts/check_pr_policy.py \
  --event-path "$GITHUB_EVENT_PATH" \
  --changed-files-file /tmp/pr-files.txt
```

CI 同时在独立的 `pr-policy` job 和现有必需的 `lint` job 中运行此检查。

## `translate_i18n.py`

通过 OpenRouter 翻译 `claude_tap/viewer_i18n.json` 中缺失的界面文本。

脚本读取查看器 i18n JSON，找到同时存在于 `en`、`zh-CN`，但在其他支持的语言（`ja`、`ko`、`fr`、`ar`、`de`、`ru`）中缺失的 key，并将翻译写回原文件。

### 前提

- 在环境中设置 `OPENROUTER_API_KEY`。
- 默认模型为 `google/gemini-2.5-flash`。

### 用法

```bash
# Show missing keys only (no file changes)
python scripts/translate_i18n.py --dry-run

# Translate missing keys and update viewer_i18n.json in place
python scripts/translate_i18n.py

# Use a specific model
python scripts/translate_i18n.py --model google/gemini-2.5-flash
```

### 高级用法

```bash
# Use a custom file/object name (future CLI i18n support)
python scripts/translate_i18n.py --target cli --dry-run
python scripts/translate_i18n.py --file claude_tap/cli.py --object-name I18N --dry-run
```

## `check_changelog.py`

检查发布 tag 是否记录在 `CHANGELOG.md` 中。发布流程检查正在发布的准确 tag。

### 用法

```bash
# Check latest release tag known to git
python scripts/check_changelog.py

# Check an explicit release tag
python scripts/check_changelog.py --tag v0.1.40
```

## `update_changelog.py`

将 `CHANGELOG.md` 和 `CHANGELOG.zh.md` 的 `## [Unreleased]` 内容转移到对应的版本章节。发布前须在两份文件中添加匹配的说明；生成器会先验证全部文档再写入。历史版本内容保持不变，也不会将 git commit 标题复制到公开发布说明中。

自动发布在打 tag 前运行此脚本。流程会创建或更新 changelog PR，等待检查通过，并在满足仓库要求时合入检查过的提交。如果仍需 review，会启用自动合入并报告等待 review。维护者须 review PR，并解决已处理的讨论；流程不使用管理员绕过。changelog PR 合入后继续发布。

### 用法

```bash
python scripts/update_changelog.py --version 0.1.40
python scripts/update_changelog.py --version 0.1.40 --date 2026-05-03
```
