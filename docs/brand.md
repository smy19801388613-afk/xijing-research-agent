# 析鲸AI · 品牌与形象

项目正式展示名称：**析鲸AI**。英文转写：**Xijing AI**。本次命名于 2026-10-16 确定；旧版验收记录中的“研序”指同一产品当时的名称。

析，代表拆解问题、核验依据；鲸，代表深入信息海洋、保持耐心与判断。产品定位是面向 A 股的可追溯投研与估值智能体。

## 形象

2026-10-16 正式采用第二轮方案 **B「花尾徽记」**。紫荆花瓣与鲸尾组成紧凑的抽象标记，中央留白呼应打开的研究书页；以克制、清楚的轮廓表达查证与研究。原深海蓝鲸鱼与观察镜方案保留为历史探索，不再作为当前产品形象。

- [正式形象 PNG](../web/public/brand/xijing-emblem.png)：透明背景，所有界面位置共用这一份资源，包括欢迎页、左上角品牌、助手头像与浏览器图标；它是栅格图，并非 SVG 矢量源文件。
- [第二轮方案与完整生成提示词](../web/public/brand/explorations-v2/README.md#b-花尾徽记)：当前形象来自 B「花尾徽记」，正式资源沿用该方案原图。

界面文字直接使用“析鲸AI”，不写进插图，避免缩放模糊和字体不一致。形象保留透明背景与原始比例，不拉伸；不同位置仅调整显示尺寸，统一使用花尾徽记。

## 颜色

产品主题沿用**紫荆紫**，按钮、链接、选中态与报告强调色统一使用原有设计系统。花尾徽记同样以紫荆紫为主色，辅以深紫与浅梅紫，统一形象与界面的配色。

| 用途 | 色值 |
|---|---|
| 紫荆紫 / 主色、按钮、链接 | `#A02070` |
| 悬停主色 | `#851B5D` |
| 深紫强调 | `#731650` |
| 浅紫背景 | `#F7EAF2` |
| 极浅紫背景 | `#FCF6FA` |
| 紫色边框 | `#E5BED5` |
| 花尾徽记 / 主色 | `#A02070` |
| 花尾徽记 / 深紫 | `#731650` |
| 花尾徽记 / 浅梅紫点缀 | `#C86BA6` |

报告 HTML 沿用紫荆紫主题与“析鲸AI”名称，保持导出自包含。

## 生成记录

当前采用方案由内置 image_gen 工具独立生成，完整提示词见[第二轮 B「花尾徽记」](../web/public/brand/explorations-v2/README.md#b-花尾徽记)。原始图位于 `explorations-v2/02-fluke-emblem.png`，正式使用资源为 `xijing-emblem.png`，均保留透明背景。

以下保留第一轮历史方案的完整提示词供追溯，**不代表当前采用方案**。第一张为新生成，第二张以第一张为身份参考进行简化；历史资源为 `xijing-mascot.png` 与 `xijing-icon.png`。

### 第一轮历史方案：主形象

```text
Use case: logo-brand
Asset type: primary mascot illustration and app identity for 析鲸AI, an A-share fundamental research assistant producing evidence-backed investment memoranda for institutional researchers.
Primary request: Design an original, distinctive intelligent whale character. One beautifully polished compact whale floating in a three-quarter side view, looking toward the viewer and slightly right. Its body is a sculpted rounded deep-ocean navy blue, with a softly luminous teal belly, a clear whale tail, small rounded pectoral fins and one very subtle dorsal fin. A slim circular turquoise optical observation lens encircles its one clearly visible bright, alert eye: it suggests examining evidence, integrated into the character rather than a handheld prop. Add only three short clean pale-teal gill-like strokes on the flank, evoking orderly research notes. Expression attentive, composed and friendly, with a very subtle closed smile. A grown-up professional companion, charming without infant proportions.
Style/medium: premium designer mascot, softly modeled 3D illustration with satin ceramic surfaces, crisp confident silhouette, minimal details, subtle tonal depth, high-quality restrained studio shading, no noisy texture.
Composition/framing: exactly one complete character, centered square canvas, character occupies about 82 percent of the canvas width and 72 percent of canvas height, sufficient clear padding on all sides, no crop. Tail and facial lens must remain legible at thumbnail size.
Color palette: deep-ocean navy #163A50, muted turquoise #4CB8B0, soft pearl #E8F4EF; tiny restrained iris highlight.
Scene/backdrop: genuinely transparent background; no ground plane, no rectangular background, no cast shadow outside character.
Text: none. No lettering, no watermark, no numbers.
Avoid: charts, coins, currency signs, stock arrows, robot antennae, suits, ties, extra characters, limbs or props, glossy plastic toy look, oversized anime eyes, neon, complex backgrounds.
```

### 第一轮历史方案：简化图标

```text
Use case: style-transfer
Asset type: small app icon and chat avatar for 析鲸AI.
Input image 1 is the identity reference and edit target: the original navy whale with a turquoise observation lens.
Transform this character into a highly legible minimal flat emblem for 32–48 px UI use. Preserve the facing direction (head right, tail left), distinctive round teal lens around the eye, navy upper body, pale turquoise belly and friendly composed expression. Compress the body to a compact near-square curled whale silhouette with a raised tail. Use only solid navy #163A50, turquoise #4CB8B0 and pearl #E8F4EF fills, no gradients, no 3D shading, no texture, no tiny highlights or three flank strokes. The eye should be one bold lens ring and a single dark dot, highly readable small. Smooth rounded shapes, precise vector-like edges, original sophisticated logo aesthetic. Exactly one mark centered on a square transparent canvas, occupying 86 percent of both width and height with even padding. No rounded-square tile, no background, no lettering, no shadow. Actual transparent background.
```
