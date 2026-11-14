# frontend/
> L2 | Parent: [../AGENTS.md](../AGENTS.md)

- `package.json`：声明类型检查和构建入口。
- `package-lock.json`：锁定开发依赖版本及完整性。
- `tsconfig.json`：定义浏览器目标与严格类型规则。
- `build.mjs`：生成 `web/experience.js`。
- [src/](src/AGENTS.md)：播放器与资料库管理源码。

运行 `npm ci --ignore-scripts`，再运行 `npm run build`。

[PROTOCOL]: Update this header on change, then check AGENTS.md
