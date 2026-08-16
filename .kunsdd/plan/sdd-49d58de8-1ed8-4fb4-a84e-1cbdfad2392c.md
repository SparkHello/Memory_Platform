# Web Console 前端韧性：错误边界与失败可见性

来源需求：`.kunsdd/requirements/49d58de8-1ed8-4fb4-a84e-1cbdfad2392c/requirement.md`（需求 A）。draft 中无 R-id 结构化需求块，步骤末尾以「验收映射」一节对应 draft 的四组验收标准，不使用 covers 标签。

## 概要

为 Memory Console（`services/memory-gateway/ui`）补齐三处前端韧性缺口：

1. **全局错误边界**：新增 React class `ErrorBoundary`，分别包裹页面渲染区、记忆档案抽屉、确认对话框，并在 `main.tsx` 加根级兜底边界；任何渲染异常不再白屏，用户可重试或返回工作室。
2. **未知 hash 提示**：`parseHash` 失败时显示「页面不存在」面板并提供返回入口，不再静默忽略。
3. **角标失败可见**：侧栏待办角标刷新非 401 失败时在导航区展示持久失败提示与重试按钮；401 仍强制回连接设置页。

只改前端，不动后端接口、不改现有 toast 与凭证感知文案体系（`utils/format.ts`）、仅中文、零新依赖。

## 现状关键事实（已核实）

- 全仓库无 `ErrorBoundary`/`componentDidCatch`；`App.tsx` 渲染 15 个页面（Dashboard/Providers/NewChannelWizard 均 45–56KB）。
- `App.tsx:211-218` `onHashChange` 中 `parseHash` 返回 `null` 时静默忽略；初始加载同理。
- `App.tsx:121-135` `refreshSignals` 吞掉非 401 错误；401 → `setCredentialsBlocked(true)`。
- 已有可复用件：`ErrorBlock`/`EmptyBlock`（`src/components/StateBlocks.tsx`）；`copyText`（`src/utils/files.ts`，已含 LAN HTTP 下 `execCommand` 回退，`tests/ui-safety.test.tsx` 有对应测试模式）。
- 测试约定：Vitest + Testing Library（`tests/`，vi.spyOn/descriptor 还原模式见 `ui-safety.test.tsx`）；Playwright e2e 用 `e2e/fakeApi.ts` 路由拦截 + `seedConsoleSettings`，`playwright.config.ts` 三个项目按 spec 文件名 testMatch（desktop-1440x900 / mobile-390x844 / mobile-375x667）。

## Pre-mortem

设想已上线但失败，倒推如下：

### Tigers（真实风险，需行动）

- **T1（Launch-Blocking）「重试」死循环**：原地重试会以同一 props 重渲染同一崩溃组件，立即再崩，用户误判功能彻底损坏。
  - 缓解：页面变体的「重试」仅清错误状态一次，再崩则回到错误页（可预期行为）；主逃生通道是「返回工作室」——先导航（`resetKeys` 变化）再由 `componentDidUpdate` 自动清错，不会回到崩溃页；悬浮层变体**不提供重试**，只提供「关闭」。Owner：前端实现者；决策日期：本计划批准时。
- **T2（Launch-Blocking）边界粒度错误**：若把 `<AppShell>` 整体包一层，抽屉/确认框崩溃会连导航一起带走，违背需求。缓解：三层边界——页面内容区、每个全局悬浮层（抽屉、确认框）各自独立、`main.tsx` 根级兜底。验收标准第 3 条锁定。Owner：前端实现者；决策日期：本计划批准时。
- **T3（Launch-Blocking）未知 hash 误伤合法链接**：编码 id、尾部斜杠、旧书签若被误判为「页面不存在」比静默更糟。缓解：仅当 hash 非空、非 `#/` 且 `parseHash` 失败时才提示；新增 `tests/navigation.test.ts` 对全部 PageKey 做 `hashForPage`/`parseHash` 往返断言并覆盖 `#/memories/<id>`、`#/knowledge/<id>`、`#/<page>?memory=<id>` 三种形态。Owner：前端实现者；决策日期：本计划批准时。
- **T4（Fast-Follow）复制诊断信息在 LAN HTTP 失败**：自部署常见非安全上下文（e2e 已记录此问题）。缓解：直接复用 `copyText`（已含 `execCommand` 回退），并在单测覆盖回退路径；若遗漏，30 天内修。
- **T5（Track）双重报警**：角标失败提示与顶栏服务状态 pill 可能同时报警。缓解：保持职责分离（pill = 服务健康；侧栏提示 = 角标数据），上线后观察是否困扰用户。

### Paper Tigers（他人可能担心，本计划不行动，仅记录以对齐）

- 「错误边界增加复杂度/新依赖」：纯 React class 组件，约 120 行，零依赖。
- 「jsdom 测不了错误边界」：可以，Testing Library 直接渲染 class 边界，只需 stub `console.error`。
- 「404 提示会破坏深链」：`parseHash` 支持的全部形态保留并有回归测试。

### Elephants（不确定，先调查再承诺）

- **E1 e2e 如何触发渲染崩溃？** 调查结论：需一个 e2e 专用探针（`import.meta.env.MODE === "e2e"` 且 `window.__CONSOLE_CRASH_PAGE__` 为真时在页面边界内抛错）；Vite 构建期会 dead-code 消除，`npm run build` 后 grep `dist/` 验证字符串不存在。→ 已解决，落入步骤 6。
- **E2 `hashForRoute` 产出的 hash 是否都能被 `parseHash` 解析（往返一致性）？** 由步骤 5 的往返单测回答；若发现不一致，按 T3 处理。

## 排序与优先级

机会分（Importance × (1 − Satisfaction)）：错误边界 9×1.0 = **9.0**；未知 hash 6×1.0 = **6.0**；角标可见 5×0.9 ≈ **4.5**。

ICE 排序（Impact/Confidence/Ease）：①错误边界组件+接线（高/高/中）→ ②未知 hash 提示（中/高/高，App.tsx 小改动）→ ③角标失败可见（中/中/中，跨 App+AppShell 且含 T5 设计点）→ 单测随各步同步 → ④e2e 最后（依赖①–③）→ ⑤全量验证。

本期 MoSCoW：

- **Must**：步骤 1–4、Vitest 用例、`npm run build` 通过。
- **Should**：Playwright 三路径（桌面+移动）、复制诊断信息 LAN 回退验证。
- **Could**：诊断信息附加 UA/uiMode 等环境信息。
- **Won't（明确不做）**：i18n、后端改动、toast 体系重构、15 个页面各自独立边界、主题跟随系统（属体验体检清单的其他需求）。

## 实施步骤

### 步骤 1：新建 `src/components/ErrorBoundary.tsx`（约 120 行）

- class 组件，state `{ error: Error | null; componentStack: string; copied: boolean }`。
- `static getDerivedStateFromError(error)` 更新 state；`componentDidCatch(error, info)` 记录 `info.componentStack` 并 `console.error("[console] 渲染异常", error, info.componentStack)`。
- Props：`variant: "page" | "overlay"`；`resetKeys?: readonly unknown[]`；`onGoHome?: () => void`（仅 page）；`onDismiss?: () => void`（仅 overlay）。
- `componentDidUpdate`：`resetKeys` 任一引用变化且当前有 error → 重置 state（这是「切换页面即恢复」的机制）。
- **page 变体** fallback（`role="alert"`，复用 `.panel`/tokens 风格）：
  - ShieldAlert 图标 + 标题「页面出现错误」+ 正文「当前页面渲染时发生异常，侧栏导航和其他功能不受影响。」
  - 错误摘要：`error.message` 截断至 200 字符，`<code>` 展示。
  - 按钮：「重试」（primary，autoFocus，清 error）、「返回工作室」（调 `onGoHome`，由父组件导航触发 resetKeys 重置）、「复制诊断信息」（调 `copyText(message + stack + componentStack + location.hash)`，成功后本地 state 显示「已复制」）。
- **overlay 变体** fallback：紧凑卡片，标题「此面板出现错误」，按钮「关闭」（调 `onDismiss`）与「复制诊断信息」；**不提供重试**（见 T1）。
- 样式追加到 `src/styles/components.css`：`.error-boundary-card` 等，复用 tokens；补 `@media (max-width: 780px)` 适配与 `prefers-reduced-motion` 降级（遵循现有文件模式）。

### 步骤 2：接线三层边界（`src/App.tsx`、`src/main.tsx`）

- `App.tsx`：把页面切换区块（dashboard…developer 整段）包成
`<ErrorBoundary variant="page" resetKeys={[activePage, knowledgeId]} onGoHome={() => navigateToPage("dashboard")}>…</ErrorBoundary>`，
作为 `AppShell` 的 children 传入——侧栏与顶栏在边界外，崩溃后导航仍可用。
- `MemoryDetailDrawer` 外包 `<ErrorBoundary variant="overlay" onDismiss={closeMemory}>`。
- `ConfirmDialog` 外包 `<ErrorBoundary variant="overlay" onDismiss={() => resolveConfirm(false)}>`。
- `main.tsx`：`<App/>` 外包根级 `<ErrorBoundary variant="page">`，其 fallback 只提供「重新加载」按钮（`location.reload()`）作为最后防线。
- `ToastView` 不加边界（约 20 行纯展示组件，根边界已兜底，YAGNI）。

### 步骤 3：未知 hash 提示（`src/App.tsx`）

- 新增 state：`const [unknownHash, setUnknownHash] = useState<string | null>(() => { const h = window.location.hash; return h && h !== "#/" && !parseHash(h) ? h : null; })`。
- `onHashChange`：`route` 为 null 时，若 hash 非空且非 `#/` → `setUnknownHash(window.location.hash)`（空 hash 仍走现有 replaceState 逻辑）；`route` 有效 → 清 `unknownHash` 并执行现有逻辑。
- `navigateToPage` 内同步清 `unknownHash`。
- 渲染：`!needsCredentialSetup && unknownHash` 时，页面区渲染「页面不存在」面板（带 PageHeader 的 panel；正文「链接可能有误或内容已移动」+ `<code>{unknownHash}</code>`；按钮「返回工作室」→ 清状态并 `navigateToPage("dashboard")`）；凭证门优先逻辑保持不变。

### 步骤 4：角标失败可见（`src/App.tsx`、`src/layout/AppShell.tsx`）

- `App.tsx`：新增 `const [signalsError, setSignalsError] = useState<string | null>(null)`；`refreshSignals` 成功路径末尾 `setSignalsError(null)`；非 401 catch 中 `setSignalsError(errorMessage(error))`。**不发 toast**——持久状态位天然满足「不刷屏」；401 分支（`setCredentialsBlocked(true)`）原样保留。
- 新增 `retrySignals`：将 `lastSignalsRefreshRef.current` 归零后 `void refreshSignals()`。
- `AppShell` 新增 props：`signalsError?: string | null`、`onRetrySignals?: () => void`。
- 桌面：nav-list 底部（模式切换按钮上方）渲染 `.nav-signals-error`（`role="status"`，警告图标 + 「待办角标暂时无法更新」+ 紧凑「重试」按钮）。移动端：`MobileMoreSheet` 内同位置复用同一小块。样式入 `components.css`。

### 步骤 5：Vitest 用例（遵循 `ui-safety.test.tsx` 的 descriptor 还原与 vi 模式）

- 新建 `tests/error-boundary.test.tsx`：
  - 可控抛错子组件抛出时 → fallback 可见（`role="alert"`、标题、错误摘要）；stub `console.error` 并断言被调用。
  - 「重试」后子组件不再抛错 → 正常渲染恢复。
  - 「返回工作室」触发 `onGoHome`；`resetKeys` 变化 → 自动重置。
  - overlay 变体：显示「关闭」，无「重试」按钮。
  - 「复制诊断信息」：mock `navigator.clipboard.writeText` 成功 → 「已复制」；reject（NotAllowedError）→ 走 `execCommand` 回退（断言与 `ui-safety` 相同的调用序列）。
- 新建 `tests/app-resilience.test.tsx`（渲染 `App`，localStorage 种子设置 + `vi.spyOn(MemoryApi.prototype, …)`）：
  - 初始 hash `#/foo` → 「页面不存在」可见；点「返回工作室」→ 工作室渲染且 hash 为 `#/studio`。
  - 运行中把 hash 改为未知值 → 同样提示；改回合法 hash → 提示消失。
  - 合法 hash 回归：`#/memories/<id>`、`#/knowledge/<id>`、`#/usage?memory=<id>` 正常打开对应页面/抽屉。
  - `memoryReport` 拒 500 → 侧栏「待办角标暂时无法更新」可见、无 error toast；点「重试」后让其成功 → 提示消失。
  - `memoryReport` 拒 401 → 仍强制回连接设置页。
- 新建 `tests/navigation.test.ts`：全部 PageKey 的 `hashForPage`/`parseHash` 往返相等；三种特殊形态解析正确（回答 E2）。

### 步骤 6：Playwright e2e（桌面 + 移动）

- **e2e 专用崩溃探针**：在 `App.tsx` 页面边界内渲染一个仅 e2e 生效的探针组件——`import.meta.env.MODE === "e2e"` 且 `window.__CONSOLE_CRASH_PAGE__` 为真时在 render 中抛错。e2e 用 `page.addInitScript` 置旗。
- `e2e/fakeApi.ts`：`FakeApiState` 增加 `failMemoryReport: boolean`；`/memories/report` 分支在该标志为真时返回 500。
- 新建 `e2e/desktop.resilience.spec.ts`，覆盖三条主路径：
  1. 置崩溃旗标后打开 `#/memories` → 错误页可见、侧栏仍可见；点「返回工作室」→ 工作室出现、全程无页面刷新（断言 `window.performance` navigation 或注入标记存活）。
  2. 打开 `#/definitely-not-a-page` → 「页面不存在」可见 → 返回工作室。
  3. `failMemoryReport = true` 打开任意页 → 侧栏失败提示可见；清标志后点「重试」→ 提示消失。
- 新建 `e2e/mobile-390.resilience.spec.ts`：移动交互路径（错误页按钮可点、底部导航仍可用、「页面不存在」面板在 390px 无横向溢出，复用 `horizontalOverflow` 模式）。
- `e2e/mobile-375.safety.spec.ts`：追加错误页与 404 面板在 375px 的 `horizontalOverflow` 断言。
- `playwright.config.ts`：desktop 项目 testMatch 放宽为 `/desktop\.(setup|resilience)\.spec\.ts/`，mobile-390 同理加入 resilience spec。

### 步骤 7：全量验证

- `cd services/memory-gateway/ui && npm run test && npm run build && npm run test:e2e`。
- `grep -r "__CONSOLE_CRASH_PAGE__" dist/` 必须无命中（验证 e2e 探针未进生产包，E1）。
- 仓库根 `scripts/test.sh`：无新增失败（后端未动，预期全绿）。

## 涉及文件

- 新增：`src/components/ErrorBoundary.tsx`、`tests/error-boundary.test.tsx`、`tests/app-resilience.test.tsx`、`tests/navigation.test.ts`、`e2e/desktop.resilience.spec.ts`、`e2e/mobile-390.resilience.spec.ts`
- 修改：`src/App.tsx`、`src/main.tsx`、`src/layout/AppShell.tsx`、`src/styles/components.css`、`e2e/fakeApi.ts`、`e2e/mobile-375.safety.spec.ts`、`playwright.config.ts`

## 验收映射（对应 draft 四组验收标准）

- **全局错误边界**（不白屏/可恢复/覆盖悬浮层/可诊断）→ 步骤 1、2；测试：步骤 5（error-boundary 用例）、步骤 6（桌面/移动崩溃路径）。
- **未知 hash 提示**（提示+合法 hash 回归）→ 步骤 3；测试：步骤 5（app-resilience、navigation 往返）、步骤 6（桌面/移动 404 路径）。
- **角标失败可见**（提示+重试、不刷屏、401 回归）→ 步骤 4；测试：步骤 5（500/401 两条用例）、步骤 6（桌面失败-重试路径）。
- **测试与验证**（Vitest、e2e 桌面+移动、build、scripts/test.sh）→ 步骤 5、6、7。

## 风险与开放问题

- T1–T3 为 Launch-Blocking，缓解已内嵌于步骤设计，无需额外工件。
- 若步骤 5 的往返测试（E2）发现 `hashForRoute` 与 `parseHash` 不一致，先修一致性问题再合入。
- 文案均为新增中文硬编码，与现状一致；若未来做 i18n，这些字符串需一并抽取（本期 Won't）。

