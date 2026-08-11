# Codegraph 使用

本项目使用 Codegraph 建立本地代码关系索引。索引目录 `.codegraph/` 仅用于本机分析，已被 Git 忽略；`docs/superpowers/` 中的本地设计与实施记录同样不会进入 Git 或 Codegraph 索引。

首次初始化：

```bash
codegraph init .
codegraph status .
```

代码变更后同步索引并检查状态：

```bash
codegraph sync .
codegraph status .
codegraph files
```

常用查询：

```bash
codegraph query ProxyRecord
codegraph query CheckerWorker
codegraph callers CheckerWorker
codegraph callees CheckerWorker
codegraph impact ProxyRecord
```

若要确认排除边界，可执行：

```bash
codegraph files | rg '\.codegraph|docs/superpowers' && exit 1 || true
git check-ignore -v .codegraph/ docs/superpowers/
```

`codegraph status .` 应显示索引为最新状态，上述 `codegraph files` 检查不应产生任何输出。不要提交 `.codegraph/`；其他开发者应在自己的工作目录内重新初始化索引。
