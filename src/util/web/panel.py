"""
Web 面板的页面。

单页、零外部依赖：容器与 NAS 上的浏览器未必能访问外网，任何 CDN 引用都会让
面板在最需要它的时候白屏。样式与脚本一律内联，二维码也由服务端直接给 SVG。

版式（流光玻璃 · 侧栏页面版）：深空底色上浮三团模糊光斑，左侧一条毛玻璃
侧栏（logo + 图标导航 + 底部账号区），主区是页切换 —— 概览 / 下载中 / 已完成 /
日志 / 命名规则 / 云端同步（CD2 配置）/ MCP 服务器 / 设置 / B站账号 各占一页。
此前下载中等内容是浮层弹出，改版后各有独立页面：下载中的进度要一直盯着、
设置改完就走、日志只在出问题时翻 —— 独立页面比浮层更贴近"它们本来就是不同
空间"的使用节奏。登录与改密码两块仍是浮层：它们是"进门"动作，不该占一个导航位。

主题跟随系统：:root 是暗色（深空 + 霓虹光斑），系统报浅色时用媒体查询整套
换到浅色变量（晨雾底 + 粉彩光斑）。纯 CSS，零 JS、零延迟。

以原始字符串存放，避免 HTML / JS 里的 `\\n`、`\\d` 被 Python 当成转义序列吃掉。

两道门在页面上分得很开：进入这个网页要面板账号（默认 admin / password），
用它去下载要 B 站账号（扫码授权）。前者是门锁，后者是钥匙串。
"""

PANEL_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bili23 Downloader</title>
<link rel="icon" type="image/x-icon" href="data:image/x-icon;base64,AAABAAEAICAAAAEAIACoEAAAFgAAACgAAAAgAAAAQAAAAAEAIAAAAAAAABAAABMLAAATCwAAAAAAAAAAAAD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A1qEAANahAADWoQAG1qEAb9ahAMvWoQD01qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD01qEAy9ahAG/WoQAG1qEAANahAADWoQAA1qEAG9ahAM/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahANDWoQAb1qEAANahAAfWoQDQ1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahANHWoQAH1qEAbtahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAG7WoQDH1qEA/9ahAP/WoQD/1qEAtdahABjWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahABvWoQC11qEA/9ahAP/WoQD/1qEAx9ahAPnWoQD/1qEA/9ahAP/WoQAZ1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahABjWoQD/1qEA/9ahAP/WoQDz1qEA/9ahAP/WoQD/1qEA/9ahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEAANahAADWoQAA1qEAANahAErWoQDn1qEA5NahAErWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAErWoQDn1qEA5NahAErWoQAA1qEAANahAADWoQAA1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQAA1qEAANahAADWoQAA1qEA5tahAP/WoQD/1qEA59ahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEA5tahAP/WoQD/1qEA59ahAADWoQAA1qEAANahAADWoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAADWoQAA1qEAANahAADWoQD/1qEA/9ahAP/WoQD/1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQD/1qEA/9ahAP/WoQD/1qEAANahAADWoQAA1qEAANahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEAANahAADWoQAA1qEAANahAP/WoQD/1qEA/9ahAP/WoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAP/WoQD/1qEA/9ahAP/WoQAA1qEAANahAADWoQAA1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQAA1qEAANahAADWoQAA1qEA5tahAP/WoQD/1qEA5tahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEA5tahAP/WoQD/1qEA5tahAADWoQAA1qEAANahAADWoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAADWoQAA1qEAANahAADWoQBJ1qEA5tahAObWoQBJ1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQBJ1qEA5tahAObWoQBJ1qEAANahAADWoQAA1qEAANahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQD/1qEA/9ahAP/WoQD/1qEA+dahAP/WoQD/1qEA/9ahABnWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAGdahAP/WoQD/1qEA/9ahAPjWoQDH1qEA/9ahAP/WoQD/1qEAttahABnWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahABnWoQC21qEA/9ahAP/WoQD/1qEAx9ahAG3WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQBt1qEABtahAM/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA0NahAAfWoQAA1qEAG9ahAM/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAM/WoQAb1qEAANahAADWoQAA1qEABtahAG7WoQDH1qEA89ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA/9ahAP/WoQD/1qEA89ahAMfWoQBu1qEABtahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEADtahAMXWoQD/1qEA/9ahAP/WoQD/1qEAxdahAA/WoQAA1qEAANahAADWoQAA1qEADtahAMXWoQD/1qEA/9ahAP/WoQD/1qEAxdahAA/WoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAAbWoQDF1qEA/9ahAP/WoQD/1qEA/9ahAMXWoQAP1qEAANahAADWoQAA1qEAANahAADWoQAA1qEADtahAMXWoQD/1qEA/9ahAP/WoQD/1qEAxdahAAbWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAYtahAP/WoQD/1qEA/9ahAP/WoQDF1qEADtahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEADtahAMXWoQD/1qEA/9ahAP/WoQD/1qEAY9ahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQBf1qEA/9ahAP/WoQD/1qEAxdahAA7WoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEADtahAMXWoQD/1qEA/9ahAP/WoQBf1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAATWoQCg1qEA6tahAKjWoQAO1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEAANahAADWoQAA1qEADtahAKjWoQDr1qEAoNahAATWoQAA1qEAANahAADWoQAA1qEAAP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A///////////AAAADgAAAAQAAAAAAAAAAA///wAf//+AP///wD///8A////AP///wDw/w8A8P8PAPD/DwDw/w8A8P8PAPD/DwD///8A////AH///gA///wAAAAAAAAAAAgAAAAcAAAAP8A8A/+AfgH/gP8B/4H/gf+D/8H/////8=">
<style>
  /* 主题变量：默认暗色（深空 + 霓虹），系统浅色时整套换成晨雾粉彩。
     两套共用同一份结构，只换颜色，不改布局。 */
  :root {
    color-scheme: dark;
    --bg: #0a0e1e;
    --panel: rgba(255,255,255,.07);
    --panel-2: rgba(255,255,255,.11);
    --line: rgba(255,255,255,.14);
    --line-soft: rgba(255,255,255,.07);
    --text: #eef1ff;
    --muted: #a9b4d6;
    --faint: #7c88ad;
    --accent: #7c3aed;
    --accent-2: #22d3ee;
    --accent-green: #34d399;
    --ok: #34d399;
    --warn: #fbbf24;
    --err: #fb7185;
    --live: #22d3ee;
    --live-glow: rgba(34,211,238,.55);
    --input: rgba(10,14,30,.45);
    --btn-bg: rgba(255,255,255,.06);
    --btn-text: #dbe4ff;
    --hover: rgba(255,255,255,.10);
    --mask: rgba(6,9,20,.55);
    --shadow: 0 18px 50px rgba(2,6,18,.5);
    --bar-track: rgba(255,255,255,.09);
    --blob1: #5b21b6;
    --blob2: #155e75;
    --blob3: #9d174d;
    --blob-op: .5;
    --grad: linear-gradient(135deg,#7c3aed,#06b6d4);
    --grad-h: linear-gradient(90deg,#8b5cf6,#22d3ee);
    --focus: rgba(124,58,237,.28);
  }
  @media (prefers-color-scheme: light) {
    :root {
      color-scheme: light;
      --bg: #e8ecf8;
      --panel: rgba(255,255,255,.62);
      --panel-2: rgba(255,255,255,.85);
      --line: rgba(255,255,255,.9);
      --line-soft: rgba(29,35,64,.08);
      --text: #232a4d;
      --muted: #5f6a95;
      --faint: #8a93b8;
      --accent: #7c3aed;
      --accent-2: #0891b2;
      --accent-green: #059669;
      --ok: #059669;
      --warn: #b45309;
      --err: #e11d48;
      --live: #0891b2;
      --live-glow: rgba(8,145,178,.35);
      --input: rgba(255,255,255,.78);
      --btn-bg: rgba(255,255,255,.7);
      --btn-text: #2c3358;
      --hover: rgba(255,255,255,.9);
      --mask: rgba(35,42,77,.32);
      --shadow: 0 14px 38px rgba(35,42,77,.22);
      --bar-track: rgba(35,42,77,.12);
      --blob1: #c4b5fd;
      --blob2: #bae6fd;
      --blob3: #fbcfe8;
      --blob-op: .6;
      --grad: linear-gradient(135deg,#7c3aed,#06b6d4);
      --grad-h: linear-gradient(90deg,#8b5cf6,#22d3ee);
      --focus: rgba(124,58,237,.22);
    }
  }

  * { box-sizing: border-box; }
  html, body { margin: 0; }
  body {
    height: 100vh; background: var(--bg); color: var(--text);
    font: 14px/1.6 -apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
    display: flex; overflow: hidden;
  }

  /* ---- 背景光斑：三团大模糊圆固定在视口，所有玻璃卡片从它们身上取色 ---- */
  .blob { position: fixed; border-radius: 50%; filter: blur(90px); z-index: 0; pointer-events: none; }
  .b1 { width: 460px; height: 460px; background: var(--blob1); top: -140px; left: -90px; opacity: var(--blob-op); }
  .b2 { width: 420px; height: 420px; background: var(--blob2); top: 200px; right: -130px; opacity: var(--blob-op); }
  .b3 { width: 340px; height: 340px; background: var(--blob3); bottom: -90px; left: 30%; opacity: var(--blob-op); }

  /* ---- 毛玻璃底座 ---- */
  .glass {
    background: var(--panel); border: 1px solid var(--line); border-radius: 18px;
    backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px);
    box-shadow: var(--shadow);
  }

  /* ---- 侧栏 ---- */
  .side {
    width: 240px; flex: none; display: flex; flex-direction: column;
    padding: 18px 14px 14px; position: relative; z-index: 1;
    border-radius: 0; border: 0; border-right: 1px solid var(--line);
    box-shadow: none;
  }
  .brand { display: flex; gap: 11px; align-items: center; padding: 2px 8px 18px; }
  /* 品牌方块：底色还是主题渐变，里面放项目自带的 bilibili 官方图标
     （就是 assets/app.svg 那个「电视头」，桌面版程序图标同款）——
     原来放的是一个白色字母 B，看着像占位符（用户 2026-10-02 要求换成 logo） */
  .mark {
    width: 36px; height: 36px; border-radius: 11px; background: var(--grad); flex: none;
    display: flex; align-items: center; justify-content: center; color: #fff;
  }
  .mark svg { width: 21px; height: 21px; display: block; fill: #fff; }
  .brand b { display: block; font-size: 14px; line-height: 1.25; letter-spacing: .3px; }
  .brand small { display: block; font-size: 10.5px; color: var(--muted); letter-spacing: .6px; }
  .nav { display: flex; flex-direction: column; gap: 3px; flex: 1; }
  .nav button {
    display: flex; align-items: center; gap: 11px; width: 100%;
    padding: 9px 12px; border: 1px solid transparent; border-radius: 11px;
    background: none; color: var(--muted); font: 500 13px/1 inherit; cursor: pointer;
    text-align: left; white-space: nowrap; transition: .15s;
  }
  .nav button svg { width: 16px; height: 16px; flex: none; }
  .nav button:hover { background: var(--hover); color: var(--text); }
  .nav button.active {
    background: rgba(139,92,246,.16); color: #c4b5fd; border-color: rgba(139,92,246,.25);
    font-weight: 600;
  }
  @media (prefers-color-scheme: light) {
    .nav button.active { background: rgba(124,58,237,.10); color: #5558e3; }
  }
  .nbadge {
    margin-left: auto; min-width: 18px; text-align: center;
    background: var(--accent-2); color: #062028;
    border-radius: 999px; font-size: 10.5px; padding: 0 6px; line-height: 16px;
    font-weight: 700; font-variant-numeric: tabular-nums;
  }
  .nbadge.zero { background: var(--panel-2); color: var(--muted); }
  .me {
    display: flex; align-items: center; gap: 8px;
    border-top: 1px solid var(--line-soft); padding: 14px 8px 2px; margin-top: 10px;
    font-size: 12.5px; color: var(--muted);
  }
  .me .dot { flex: none; }
  .me #loginText { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  /* ---- 主区：页切换 ---- */
  main { flex: 1; overflow-y: auto; overflow-x: hidden; position: relative; z-index: 1; }
  .page { display: none; max-width: 1020px; margin: 0 auto; padding: 24px 28px 56px; }
  .page.on { display: block; }
  .phead { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
  .phead h1 { margin: 0; font-size: 20px; font-weight: 700; letter-spacing: .3px; }
  .phead .count { color: var(--muted); font-size: 13px; }
  .pacts { margin-left: auto; display: flex; gap: 8px; flex-wrap: wrap; }

  /* ---- 概览：添加下载（Hero） ---- */
  .hero { padding: 22px 24px; }
  .hero h1 { margin: 0; font-size: 21px; font-weight: 700; letter-spacing: .3px; }
  .hero p { margin: 5px 0 0; color: var(--muted); font-size: 12.5px; }
  .add { display: flex; gap: 10px; margin-top: 16px; flex-wrap: wrap; }
  .add input {
    flex: 1; min-width: 240px; font: inherit; font-size: 13.5px; padding: 13px 16px;
    border-radius: 12px; border: 1px solid var(--line); background: var(--input);
    color: var(--text); outline: none; transition: .15s;
  }
  .add input::placeholder { color: var(--faint); }
  .add input:focus { border-color: var(--accent-2); box-shadow: 0 0 0 3px var(--focus); }

  /* ---- 指标卡 ---- */
  .metrics { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-top: 16px; }
  .met { padding: 16px 18px; }
  .met .lab { font-size: 11.5px; color: var(--muted); letter-spacing: 1px; }
  .met b {
    display: block; font-size: 26px; font-weight: 700; margin-top: 6px;
    font-variant-numeric: tabular-nums; line-height: 1.2;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .met small { font-size: 11.5px; color: var(--accent-green); display: block; margin-top: 3px; }
  .met small.dim { color: var(--muted); }
  .met small.unit { display: inline; color: var(--muted); font-size: 13px; font-weight: 500; }

  .dir {
    display: flex; align-items: center; gap: 10px; padding: 12px 18px; margin-top: 12px;
    font-size: 12.5px; color: var(--muted); flex-wrap: wrap;
  }
  .dir b { color: var(--text); font-weight: 500; word-break: break-all; }

  /* ---- 队列面板 / 通用面板 ---- */
  .panel { margin-top: 16px; padding-bottom: 6px; }
  .panel .h {
    display: flex; align-items: baseline; gap: 10px; padding: 14px 20px 8px;
    font-size: 14px; font-weight: 700;
  }
  .panel .h .sub { margin-left: auto; font-weight: 400; font-size: 11.5px; color: var(--muted); }

  /* 可折叠分组：整页一张卡，里面一项一行 —— 点行头收起/展开，默认收起。
     收起用 `> *:not(.h)` 一次性藏掉正文，行内部结构一行都不用动，
     里面的 id（事件绑定、渲染目标）也不会因为多包一层而失效 */
  .nt-acc { padding: 5px 0; }
  .nt-item { border-top: 1px solid var(--line-soft); }
  .nt-item:first-child { border-top: 0; }
  .nt-item > .h {
    display: flex; align-items: center; gap: 12px;
    padding: 12px 20px; cursor: pointer; user-select: none;
    transition: background .15s;
  }
  .nt-item > .h:hover, .nt-item:not(.collapsed) > .h { background: var(--hover); }
  /* 标题与副标题各占一行 —— 挤在同一行里，右半边会闹成一团 */
  .nt-t { flex: 1; min-width: 0; }
  .nt-t b { display: block; font-size: 13.5px; font-weight: 600; }
  .nt-t small {
    display: block; margin-top: 3px; font-size: 11.5px;
    font-weight: 400; color: var(--muted);
  }
  .nt-item:not(.collapsed) > .h .nt-t b { color: var(--accent); }
  .nt-item > .h .fchev {
    flex: none; font-style: normal; font-size: 11px; color: var(--muted);
    transition: transform .15s;
  }
  .nt-item.collapsed > .h .fchev { transform: rotate(-90deg); }
  .nt-item > .h:hover .fchev { color: var(--accent); }
  .nt-item.collapsed > *:not(.h) { display: none; }
  /* 收起时得看得见关键状态，否则一排光标题等于没信息 */
  .ntbadge {
    flex: none; display: inline-flex; align-items: center; gap: 5px;
    font-size: 11px; font-weight: 500; padding: 2px 9px; border-radius: 999px;
    white-space: nowrap; border: 1px solid var(--line); color: var(--muted);
  }
  .ntbadge::before {
    content: ""; width: 5px; height: 5px; border-radius: 50%;
    background: currentColor; opacity: .75;
  }
  .ntbadge.on { color: var(--accent-green); border-color: var(--accent-green); }

  .panel:first-child, .page > .panel + .panel { margin-top: 0; }
  .page .panel + .panel { margin-top: 16px; }
  .hero + .panel { margin-top: 16px; }

  /* 通用横向行。`align-items: center` 不能省 —— flex 的默认值是 stretch，
     行里比按钮矮的元素（计数、说明文字）会被**纵向拉满**成一团空白。 */
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .row-end { display: flex; gap: 8px; justify-content: flex-end; margin-top: 16px; }
  input[type=text], input[type=password] {
    font: inherit; flex: 1; min-width: 0; width: 100%; padding: 10px 13px;
    border-radius: 10px; border: 1px solid var(--line); background: var(--input);
    color: var(--text); outline: none; transition: .15s;
  }
  input[type=text]:focus, input[type=password]:focus { border-color: var(--accent-2); box-shadow: 0 0 0 3px var(--focus); }

  .ep { display: flex; align-items: center; gap: 10px; padding: 9px 14px; border-radius: 10px; }
  .ep:hover { background: var(--hover); }
  .ep input { width: 16px; height: 16px; accent-color: var(--accent); }
  .ep .t { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ep .id { color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
  /* ---- 解析结果的分组树 ----
     父行（类别/剧名、章节）与条目行共用同一套行高与内缩基线，靠 .gbody 的左内缩与
     竖线表示层级。没有这一层时，番剧的「正片 / 相关推荐 / 重温原著…」和 157 条
     剧集糊在一起，哪几条属于哪一段完全看不出来 */
  .ghead { display: flex; align-items: center; gap: 10px; padding: 9px 14px; border-radius: 10px; }
  .ghead:hover { background: var(--hover); }
  .ghead .gtog {
    flex: none; width: 20px; height: 20px; padding: 0; border: 0; background: transparent;
    color: var(--muted); font-size: 11px; line-height: 1; border-radius: 6px;
  }
  .ghead .gtog:hover { background: var(--hover); color: var(--text); }
  .ghead input { flex: none; width: 16px; height: 16px; accent-color: var(--accent); }
  .ghead .gnum {
    flex: none; font-size: 11px; color: var(--muted);
    border: 1px solid var(--line); border-radius: 999px; padding: 1px 8px;
  }
  .ghead .gttl { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
  .ghead .gcnt { flex: none; color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
  /* 缩进 + 一根竖线：层级靠位置表达，不靠字号，省得每层一个字号 */
  .gbody { margin-left: 24px; padding-left: 18px; border-left: 1px solid var(--line); }
  .ggrp.collapsed > .gbody { display: none; }
  /* 批量解析的文本框与解析记录列表 */
  .batchbox {
    width: 100%; font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
    font-size: 12px; line-height: 1.7; padding: 10px 13px; resize: vertical;
    border-radius: 10px; border: 1px solid var(--line); background: var(--input);
    color: var(--text); outline: none; transition: .15s;
  }
  .batchbox:focus { border-color: var(--accent-2); box-shadow: 0 0 0 3px var(--focus); }
  .batchauto { display: flex; align-items: center; gap: 8px; margin-top: 12px; font-size: 13px; color: var(--muted); }
  .histlist { max-height: 46vh; overflow: auto; margin-top: 6px; }
  .hrow { display: flex; align-items: center; gap: 10px; padding: 8px 10px; border-radius: 10px; }
  .hrow:hover { background: var(--hover); }
  .hrow .hidx { flex: none; width: 26px; color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
  .hrow .httl { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .hrow .htyp, .hrow .htime { flex: none; color: var(--muted); font-size: 12px; }
  .hrow .htime { font-variant-numeric: tabular-nums; }

  /* 收藏页：三个分类胶囊 + 卡片网格。展开的内容块按 `grid-column: 1/-1`
     独占一整行插在卡片后面 —— 不让卡片自己撑开，否则整片网格会跟着重排 */
  .favtabs { display: flex; align-items: center; gap: 7px; padding: 14px 18px 0; flex-wrap: wrap; }
  .favtabs button {
    display: inline-flex; align-items: center; height: 28px;
    padding: 0 14px; border-radius: 999px; font-family: inherit; font-size: 12.5px;
    border: 1px solid var(--line); background: transparent; color: var(--muted); cursor: pointer;
    transition: color .15s, border-color .15s;
  }
  .favtabs button:hover { color: var(--text); border-color: var(--accent); }
  .favtabs button.active {
    color: #fff; border-color: transparent;
    background: linear-gradient(135deg, #7c3aed, #6366f1 55%, #06b6d4);
  }
  .favgrid {
    display: grid; align-items: start; gap: 12px;
    grid-template-columns: repeat(auto-fill, minmax(238px, 1fr));
    padding: 14px 18px 6px;
  }
  .favgrid .empty { grid-column: 1 / -1; padding-left: 2px; }
  /* 同系列多季的下拉 —— 就是桌面端长在解析页左下角、能"解析好几部"的那个。
     只在真有第二季时才出现，所以别给它固定宽度 */
  .favseason { font-size: 12px; padding: 4px 8px; border-radius: 8px; max-width: 200px; }
  .favcard {
    display: flex; align-items: center; gap: 11px; padding: 10px;
    border: 1px solid var(--line); border-radius: 12px;
    background: var(--panel-2); cursor: pointer;
    transition: border-color .15s;
  }
  .favcard:hover { border-color: var(--accent); }
  .favcard .fc {
    position: relative; flex: 0 0 auto; width: 54px; height: 54px; border-radius: 9px;
    overflow: hidden; display: flex; align-items: center; justify-content: center;
    background: var(--panel); color: var(--faint);
  }
  /* 首字占位在下、封面盖在上面：封面加载不出来（容器出不了外网是常态）
     就自己移除，露出下面那层，不会留一块空白 */
  .favcard .fc img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; display: block; }
  .favcard .fc b { font-size: 19px; font-weight: 600; }
  .favcard .ft { flex: 1; min-width: 0; }
  .favcard .ft b {
    display: block; font-size: 13px; font-weight: 600;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .favcard .ft span { display: block; margin-top: 4px; font-size: 11.5px; color: var(--muted);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .favep {
    grid-column: 1 / -1; border: 1px solid var(--line); border-radius: 12px;
    background: var(--panel); padding: 4px 8px 10px;
  }
  .favep .bar { display: flex; align-items: center; gap: 8px; padding: 8px 6px 6px; flex-wrap: wrap; }
  .favep .bar .t { font-size: 12.5px; color: var(--muted); }
  .favep .bar .spacer { flex: 1; }
  .favep .eps { max-height: 316px; overflow: auto; }

  /* ---- 任务行：状态光点 + 大百分比 ---- */
  .task { padding: 13px 20px; border-top: 1px solid var(--line-soft); }
  .task:first-child { border-top: 0; }
  .task .head { display: flex; align-items: center; gap: 10px; }
  .tdot { width: 9px; height: 9px; border-radius: 50%; flex: none; background: var(--faint); }
  .task.s-downloading .tdot, .task.s-parsing .tdot, .task.s-merging .tdot,
  .task.s-converting .tdot, .task.s-additional_processing .tdot, .task.s-ffmpeg_queued .tdot {
    background: var(--live); box-shadow: 0 0 10px var(--live-glow);
  }
  .task.s-paused .tdot { background: var(--warn); }
  .task.s-queued .tdot { background: var(--faint); }
  .task.s-failed .tdot, .task.s-ffmpeg_failed .tdot, .task.s-invalid .tdot { background: var(--err); }
  .task.done .tdot { background: var(--accent-green); }
  .task .name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
  .task .meta { color: var(--muted); font-size: 11.5px; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .task .meta b { color: var(--ok); font-weight: 600; }
  .task .pct {
    flex: none; min-width: 44px; text-align: right; font-size: 17px; font-weight: 700;
    font-variant-numeric: tabular-nums;
  }
  .bar { height: 6px; border-radius: 3px; background: var(--bar-track); margin-top: 9px; overflow: hidden; }
  .bar > i { display: block; height: 100%; border-radius: 3px; background: var(--grad-h); transition: width .4s; }
  .task.s-paused .bar > i { background: var(--warn); }
  .task.s-failed .bar > i, .task.s-ffmpeg_failed .bar > i, .task.s-invalid .bar > i { background: var(--err); }
  .task.done .bar > i { background: var(--accent-green); }
  .task .acts { display: flex; gap: 6px; margin-top: 10px; }
  .tag {
    font-size: 11px; padding: 2px 8px; border-radius: 999px; background: var(--panel-2);
    color: var(--muted); white-space: nowrap; flex: none;
  }
  .tag.running { background: rgba(34,211,238,.16); color: var(--live); }
  .tag.paused { background: rgba(251,191,36,.16); color: var(--warn); }
  .tag.failed { background: rgba(251,113,133,.16); color: var(--err); }

  /* ---- 「N 个条目」这类计数：与 .phead .count 同一个语汇（静音小字 + 数字加重）----
     🔴 别拿 .tag 来当计数。.tag 是**任务行里的状态胶囊**（11px、圆角 999px），
     摆进 .row 这种 flex 行会被拉成一颗 64×42 的空药丸（字只占中间一小条），
     与旁边 14px 的按钮完全不搭 —— 用户 2026-10-02 圈出来说"显示不协调"的就是它。 */
  .cnt { font-size: 13px; color: var(--muted); font-variant-numeric: tabular-nums; }
  .cnt b { color: var(--text); font-weight: 600; margin-right: 3px; }

  .empty { color: var(--muted); font-size: 13px; padding: 10px 20px; }

  /* ---- 分页条（已完成列表）----
     与任务行同一套内边距（20px），翻页按钮贴在左边、页码信息推到最右 */
  .pager {
    display: flex; align-items: center; gap: 8px;
    padding: 13px 20px 3px; border-top: 1px solid var(--line-soft);
  }
  .pager .pginfo {
    margin-left: auto; color: var(--muted); font-size: 12px;
    font-variant-numeric: tabular-nums;
  }
  .err { color: var(--err); font-size: 13px; }
  .ok { color: var(--ok); }
  .warn { color: var(--warn); }
  .muted { color: var(--muted); font-size: 13px; }

  /* ---- 表单字段（云端同步页） ---- */
  .field { margin-bottom: 14px; }
  .field label { display: block; font-size: 12px; color: var(--muted); margin-bottom: 5px; }
  .field .hint { font-size: 11px; color: var(--faint); margin-top: 4px; }
  .field input { width: 100%; }
  /* 卡片内部的说明段。它曾经是卡片**外面**独立的一小块（.howto），实测两者
     的间距本来就被压到 0 —— 看着"分成两块"其实来自两张卡各自的边框与阴影。
     移进卡片内部后少一层壳，说明在视觉上也归到了它所解释的那组配置里 */
  .howto-in {
    color: var(--muted); font-size: 12.5px; line-height: 1.8;
    margin: 0 20px; padding: 13px 0 12px; border-top: 1px solid var(--line-soft);
  }
  .howto-in code {
    background: var(--input); border: 1px solid var(--line);
    border-radius: 5px; padding: 1px 6px; font-size: 11px;
  }
  .cfgfoot { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .cfgfoot .msg { color: var(--muted); font-size: 11.5px; }

  /* ---- 通知 ---- */
  /* 凭据输入框 + 显示/隐藏按钮并排 */
  .ntsecret { display: flex; align-items: center; gap: 8px; }
  .ntsecret input { flex: 1; }
  /* 最近发送记录：一行一条，成功/失败用色区分。日期与说明可能很长，
     给说明留换行权（详情里常有 Telegram 返回的长句） */
  .ntlog {
    display: flex; align-items: baseline; gap: 10px;
    padding: 8px 0; border-top: 1px solid var(--line-soft); font-size: 12px;
  }
  .ntlog:first-child { border-top: 0; }
  .ntlog b { flex: none; }
  .ntlog b.ok { color: var(--ok); }
  .ntlog b.bad { color: var(--err); }
  .ntlog .ch { flex: none; color: var(--text); }
  .ntlog .tm { flex: none; color: var(--faint); }
  .ntlog .dt { color: var(--muted); word-break: break-all; }
  /* .cfgfoot .msg 的灰字特异性更高，测试结果的成败色要再点一层才压得住 */
  .cfgfoot .msg.ok { color: var(--ok); }
  .cfgfoot .msg.err { color: var(--err); }

  /* ---- 设置 ---- */
  input[type=number] {
    font: inherit; padding: 8px 11px; border: 1px solid var(--line); border-radius: 10px;
    background: var(--input); color: var(--text); outline: none; transition: .15s;
  }
  input[type=number]:focus { border-color: var(--accent-2); box-shadow: 0 0 0 3px var(--focus); }
  .setrow {
    display: flex; align-items: center; gap: 16px;
    padding: 12px 0; border-top: 1px solid var(--line-soft);
  }
  .setrow:first-child { border-top: 0; }
  /* 优先级行展开后控件列很高，标签顶对齐贴着摘要按钮，别悬在半空 */
  .setrow.prio-row { align-items: flex-start; }
  .setrow.prio-row > div:first-child { padding-top: 7px; }
  .setgroup {
    font-size: 12px; font-weight: 600; letter-spacing: .06em;
    color: var(--muted); margin: 18px 0 6px;
    padding-bottom: 6px; border-bottom: 1px solid var(--line);
  }
  .setgroup:first-child { margin-top: 4px; }
  /* 可折叠组头：整组行挂 data-g，点击组头切换 gfold；状态存 localStorage */
  .setgroup.fold { cursor: pointer; display: flex; align-items: center; user-select: none; padding: 6px 0; }
  .setgroup.fold:hover { color: var(--text); }
  .setgroup.fold .fchev {
    margin-left: auto; font-style: normal; font-size: 12px; color: var(--muted);
    transition: transform .15s; padding: 2px 6px;
  }
  .setgroup.fold:hover .fchev { color: var(--text); }
  .setgroup.fold.folded .fchev { transform: rotate(-90deg); }
  .setrow.gfold { display: none; }
  /* 非手动代理模式下，服务器/端口/账号/密码整行灰显且禁用 */
  .setrow.dim { opacity: .45; }
  .setrow.dim input { opacity: .6; }
  .setname { font-size: 13px; }
  .sethint { color: var(--muted); font-size: 12px; margin-top: 2px; }
  .setctl { margin-left: auto; flex: none; display: flex; align-items: center; gap: 6px; }
  .setctl input[type=number] { width: 104px; text-align: right; }
  .setctl input[type=checkbox] { width: 18px; height: 18px; accent-color: var(--accent); margin: 0; }
  .setctl select {
    background: var(--input); color: var(--text);
    border: 1px solid var(--line); border-radius: 8px;
    padding: 6px 10px; font-size: 13px; min-width: 120px;
    outline: none;
  }
  .setctl select:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--focus); }

  /* MCP 服务器组：状态光点复用任务行那套语义色，令牌与客户端配置是只读代码块 */
  .mcp-status { display: flex; align-items: center; gap: 8px; font-size: 13px; }
  .mcp-dot { width: 8px; height: 8px; border-radius: 50%; flex: none; background: var(--muted); }
  .mcp-dot.on { background: var(--ok); box-shadow: 0 0 6px var(--ok); }
  .mcp-dot.off { background: var(--muted); }
  .mcp-dot.err { background: var(--err); box-shadow: 0 0 6px var(--err); }
  .mcp-code {
    font-family: ui-monospace, Consolas, monospace; font-size: 12px;
    background: var(--input); color: var(--text);
    border: 1px solid var(--line); border-radius: 8px;
    padding: 6px 10px; max-width: 340px; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap;
  }
  .mcp-pre {
    font-family: ui-monospace, Consolas, monospace; font-size: 12px;
    background: var(--input); color: var(--text);
    border: 1px solid var(--line); border-radius: 8px;
    padding: 10px 12px; margin: 0; overflow: auto; max-height: 180px;
    white-space: pre; text-align: left;
  }
  .mcp-widen { flex-direction: column; align-items: stretch; gap: 8px; }
  .mcp-widen .setctl { margin-left: 0; width: 100%; align-items: flex-start; }
  .mcp-widen .mcp-pre { flex: 1; min-width: 0; }

  /* 优先级排序：默认收起只看摘要，点开拖拽（或箭头）调序，顺序存在隐藏框里 */
  .prio { width: 250px; }
  /* 服务商 CDN 列表常开、更宽，行内是「服务商 · 主机」长名 */
  .prio.cdn { width: 100%; max-width: 430px; }
  .priosum {
    display: flex; align-items: center; gap: 8px; width: 100%;
    background: var(--input); border: 1px solid var(--line-soft);
    border-radius: 10px; padding: 8px 10px;
    color: var(--text); font: inherit; font-size: 12px;
    cursor: pointer; text-align: left;
  }
  .priosum:hover { border-color: var(--accent-2); }
  .priosum .sumt { flex: 1 1 auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .priosum .chev { flex: none; color: var(--muted); font-size: 10px; transition: transform .15s; }
  .prio.open .priosum .chev { transform: rotate(180deg); }
  .priolist { display: none; flex-direction: column; gap: 4px; margin-top: 6px; }
  .prio.open .priolist { display: flex; }
  .priow {
    display: flex; align-items: center; gap: 8px;
    background: var(--input); border: 1px solid var(--line-soft);
    border-radius: 8px; padding: 4px 6px 4px 4px;
  }
  .priow.dragging { opacity: .4; border-style: dashed; }
  .grip {
    flex: none; cursor: grab; color: var(--muted); font-size: 11px;
    letter-spacing: -2px; user-select: none; padding: 0 1px;
  }
  .priow.dragging .grip { cursor: grabbing; }
  .prio-n {
    flex: none; width: 20px; height: 20px; border-radius: 6px;
    background: var(--btn-bg); color: var(--muted);
    font-size: 11px; display: flex; align-items: center; justify-content: center;
    font-variant-numeric: tabular-nums;
  }
  .prio-t {
    flex: 1 1 auto; font-size: 12px; color: var(--text);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .pi {
    flex: none; width: 22px; height: 22px; padding: 0; line-height: 1;
    border-radius: 6px; border: 1px solid var(--line);
    background: var(--btn-bg); color: var(--btn-text);
    cursor: pointer; font-size: 11px;
  }
  .pi:hover:not(:disabled) { background: var(--hover); }
  .pi:disabled { opacity: .35; cursor: default; }

  .unit { color: var(--muted); font-size: 12px; min-width: 34px; }
  .roval {
    margin-left: auto; color: var(--muted); font-size: 12px; text-align: right;
    word-break: break-all; max-width: 62%; font-variant-numeric: tabular-nums;
  }

  /* ---- 扫码 ---- */
  .scan { display: flex; gap: 20px; align-items: flex-start; flex-wrap: wrap; }
  .qr {
    width: 200px; height: 200px; flex: none; padding: 10px;
    border: 1px solid var(--line); border-radius: 14px; background: #fff;
    display: flex; align-items: center; justify-content: center;
  }
  .qr svg { width: 100%; height: 100%; display: block; }
  .qr .empty { text-align: center; padding: 0; color: #8b949e; }
  .scan-info { flex: 1; min-width: 200px; }
  .scan-info p { margin: 0 0 12px; }

  /* ---- 日志 ---- */
  .logbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
  select {
    font: inherit; font-size: 13px; padding: 7px 10px; border-radius: 10px;
    border: 1px solid var(--line); background: var(--input); color: var(--text);
    cursor: pointer; outline: none;
  }
  select:focus { border-color: var(--accent-2); }
  .chk { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: var(--muted); cursor: pointer; }
  .chk input { width: 15px; height: 15px; accent-color: var(--accent); margin: 0; }
  .logbox {
    margin: 0; padding: 14px; border-radius: 12px;
    background: rgba(8,12,26,.88); color: #cdd6f4; border: 1px solid rgba(255,255,255,.08);
    font: 12px/1.55 ui-monospace, Consolas, "Cascadia Mono", "Courier New", monospace;
    white-space: pre-wrap; word-break: break-word;
    max-height: min(60vh, 640px); overflow: auto;
  }
  .logbox .lv-err { color: #fda4af; }
  .logbox .lv-warn { color: #fcd34d; }
  .logbox .lv-dim { color: #6c738f; }

  /* ---- 登录 / 改密码浮层：进门动作不占导航位 ---- */
  .overlay {
    position: fixed; inset: 0; z-index: 50; padding: 20px;
    background: var(--mask);
    backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px);
    display: flex; align-items: center; justify-content: center;
  }
  .overlay .box { padding: 26px; width: min(420px, 100%); }
  .overlay h2 { margin: 0 0 6px; font-size: 16px; }
  .overlay p { color: var(--muted); font-size: 13px; margin: 0 0 14px; }

  button {
    font: inherit; cursor: pointer; border-radius: 12px; border: 1px solid var(--line);
    background: var(--btn-bg); color: var(--btn-text); padding: 9px 16px; transition: .15s;
  }
  button:hover { border-color: var(--faint); background: var(--hover); }
  button.primary {
    /* 🔴 边框不能写 `border: 0` —— 主按钮通常是"实心无边框"的视觉，但它和旁边的
       次按钮并排时，少了上下各 1px 就会**矮 2px**（实测 40.4 vs 42.4），
       行内看起来一颗高一顆低。留一条透明边框，视觉一样、高度与别人齐。 */
    background: var(--grad); border: 1px solid transparent; color: #fff; font-weight: 700;
    box-shadow: 0 8px 24px rgba(93,52,214,.35);
  }
  button.primary:hover { filter: brightness(1.12); }
  button:disabled { opacity: .5; cursor: not-allowed; }
  button.sm { padding: 6px 12px; font-size: 12.5px; border-radius: 9px; }
  button.sm.danger { color: var(--err); border-color: color-mix(in srgb, var(--err) 40%, transparent); }
  button.sm.danger:hover { background: color-mix(in srgb, var(--err) 12%, transparent); }
  /* 小方形图标按钮：侧栏底部的「退出登录」、账户页的「修改密码」还在用 */
  .ico-btn {
    width: 32px; height: 32px; border-radius: 10px; border: 1px solid var(--line);
    background: var(--btn-bg); color: var(--muted); display: inline-flex; align-items: center;
    justify-content: center; cursor: pointer; flex: none; font-size: 14px; transition: .15s;
  }
  .ico-btn:hover { background: var(--hover); color: var(--text); }

  /* ---- 圆形图标按钮：渐变描边环 + 线性渐变图标（概览页右上角的动作按钮）----
     环是"padding 手法"画的：外层吃渐变当底色，::before 铺一层内圆把中间盖掉，
     剩下的那一圈就是描边 —— 纯 CSS，不靠 svg 描边也能做出渐变色环。
     图标本身跟环共用同一套渐变色（描边引 body 里那份 defs）。
     配色 = 主题 --grad 那套「紫 #7c3aed → 青 #06b6d4」，中站插一个靛蓝让过渡不发灰。
     曾经用过粉→紫→靛那一版（色号见 git log），在浅色主题下比页面本身艳一档，
     看着跳；换成主题同族色才嵌得进去（用户 2026-10-02 明确要求）。 */
  .ring-btn {
    width: 38px; height: 38px; padding: 0; border: 0; border-radius: 50%;
    background: linear-gradient(135deg, #7c3aed 0%, #6366f1 48%, #06b6d4 100%);
    display: inline-grid; place-items: center; position: relative; flex: none;
    cursor: pointer; transition: transform .15s, filter .15s;
  }
  .ring-btn::before {
    content: ""; position: absolute; inset: 1.5px; border-radius: 50%;
    background: var(--btn-bg); backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px); transition: background .15s;
  }
  .ring-btn > svg { position: relative; width: 17px; height: 17px; display: block; }
  .ring-btn:hover { transform: translateY(-1px); filter: brightness(1.12); }
  .ring-btn:hover::before { background: var(--hover); }
  .ring-btn:active { transform: translateY(0) scale(.94); }
  .ring-btn:disabled { opacity: .45; cursor: not-allowed; transform: none; filter: none; }
  .ring-btn.spin > svg { animation: ring-spin .7s linear infinite; }
  @keyframes ring-spin { to { transform: rotate(360deg); } }

  .hidden { display: none !important; }
  .spacer { flex: 1; }

  #toast {
    position: fixed; left: 50%; bottom: 28px; transform: translateX(-50%) translateY(80px);
    background: var(--panel-2); border: 1px solid var(--line); color: var(--text);
    backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px);
    padding: 10px 20px; border-radius: 999px;
    font-size: 13px; opacity: 0; transition: .25s; pointer-events: none; max-width: 90vw;
    z-index: 60; box-shadow: var(--shadow);
  }
  #toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }

  /* ---- 命名规则：左边规则表、右边编辑器 ----
     不做桌面端那套可视化片段编辑器（片段 ↔ 规则串的双向映射 + 拖拽），
     网页里要的是「改一条、立刻看到它渲染成什么」，所以右边就是规则串 + 变量条 + 预览 */
  .nmwrap { display: flex; gap: 16px; padding: 6px 20px 0; align-items: flex-start; }
  .nmlist { width: 296px; flex: none; display: flex; flex-direction: column; gap: 8px; }
  .nmbar { display: flex; gap: 6px; }
  .nmbar input { flex: 1; min-width: 0; }
  .nmitems {
    display: flex; flex-direction: column; gap: 3px;
    max-height: 470px; overflow-y: auto; padding-right: 2px;
  }
  .nmi {
    display: flex; align-items: center; gap: 8px; width: 100%;
    padding: 8px 10px; border-radius: 10px;
    border: 1px solid transparent; background: transparent;
    color: var(--text); font: inherit; font-size: 13px; text-align: left;
  }
  .nmi:hover { background: var(--hover); border-color: var(--line-soft); }
  .nmi.on { background: var(--input); border-color: var(--accent-2); }
  .nmi .t { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .nmi .ty { flex: none; font-size: 11px; color: var(--muted); }
  .nmi .df { flex: none; color: var(--ok); font-size: 11px; }
  .nmi.bad .t { color: var(--err); }
  .nmedit { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 11px; }
  .nmedit > .chk { font-size: 12.5px; }
  .nmrow { display: flex; gap: 10px; flex-wrap: wrap; }
  .nmf { display: flex; flex-direction: column; gap: 5px; flex: 1; min-width: 140px; }
  .nmf.nmfull { flex-basis: 100%; }
  .nmf label { font-size: 12px; color: var(--muted); }
  .nmf input, .nmf select, .nmf textarea { width: 100%; }
  /* 规则串动辄上百字符（影视那条 170+），单行 input 只能看到开头一截，改都没法改。
     给成多行等宽、可纵向拖高。textarea 吃不到全局的 input[type=text] 样式，得补齐 */
  .nmf textarea {
    font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
    font-size: 12px; line-height: 1.7; padding: 10px 13px; resize: vertical;
    border-radius: 10px; border: 1px solid var(--line); background: var(--input);
    color: var(--text); outline: none; transition: .15s;
  }
  .nmf textarea:focus { border-color: var(--accent-2); box-shadow: 0 0 0 3px var(--focus); }
  .nmf .hint { font-size: 11px; color: var(--faint); line-height: 1.5; }
  .nmf .hint.bad { color: var(--err); }
  /* 输入框 + 按钮并排。上面 .nmf input 给的是 width:100%，这里要显式收回，
     否则 flex 元素按 100% 铺满，按钮会被挤出这一行 */
  .nminp { display: flex; gap: 8px; align-items: center; }
  .nminp input { flex: 1 1 auto; width: auto; min-width: 0; }
  .nminp button { flex: none; }
  .nmvlabel { color: var(--faint); font-weight: 400; }
  .nvars { display: flex; flex-wrap: wrap; gap: 5px; max-height: 150px; overflow-y: auto; padding: 2px 2px 2px 0; }
  .nvc {
    border: 1px solid var(--line-soft); background: var(--input); color: var(--text);
    border-radius: 8px; padding: 3px 8px; font-size: 11.5px; cursor: pointer;
    font-family: ui-monospace, Consolas, monospace; white-space: nowrap;
  }
  .nvc:hover { border-color: var(--accent-2); background: var(--hover); }
  .nvc i { font-style: normal; color: var(--muted); margin-left: 6px; }
  .nmpv { display: flex; flex-direction: column; gap: 5px; }
  .nmpv .pvr { display: flex; gap: 10px; align-items: baseline; }
  .nmpv .pvl { flex: none; width: 72px; color: var(--muted); font-size: 11.5px; }
  .nmpv .pvv {
    flex: 1; min-width: 0; word-break: break-all; font-size: 12.5px;
    font-family: ui-monospace, Consolas, monospace;
  }
  .nmpv .pvv.bad { color: var(--err); }
  .nmmixed { font-size: 11.5px; color: var(--faint); }

  /* ---- 名称识别 ---- */
  /* 预览要把「识别前 / 识别后」并排讲清楚：命中后哪些字段被改写、文件名变成什么。
     改动的行单独上色，否则两组值看起来只是重复了一遍 */
  .idopt { margin-left: 6px; font-size: 11px; color: var(--faint); }
  .idpv { display: flex; flex-direction: column; gap: 8px; }
  .idcmp {
    display: flex; flex-direction: column; gap: 3px;
    padding: 9px 11px; border: 1px solid var(--line-soft);
    border-radius: 9px; background: var(--input);
  }
  .idcmp .h { font-size: 11px; color: var(--faint); margin-bottom: 2px; }
  .idline { display: flex; gap: 10px; align-items: baseline; font-size: 12px; }
  .idline .k { flex: none; width: 84px; color: var(--muted); }
  .idline .v { flex: 1; min-width: 0; word-break: break-all; }
  .idline .v.chg { color: var(--ok); font-weight: 600; }
  .idline .v.nil { color: var(--faint); }
  .idline .v.pth { font-family: ui-monospace, Consolas, monospace; font-size: 12px; }
  .idline .v.pth.bad { color: var(--err); }

  @media (max-width: 880px) {
    .nmwrap { flex-direction: column; }
    .nmlist { width: 100%; }
    .nmitems { max-height: 220px; }
  }

  /* ---- 窄屏：侧栏收成图标条，页面占满剩余宽度 ---- */
  @media (max-width: 880px) {
    .side { width: 60px; padding: 18px 8px 14px; }
    .brand { padding: 2px 0 18px; justify-content: center; }
    .brand div { display: none; }
    .nav button { justify-content: center; padding: 10px 0; }
    .nav button svg { width: 18px; height: 18px; }
    .nav button span, .me #loginText { display: none; }
    .nbadge { position: absolute; margin: 0; transform: translate(14px, -8px); }
    .nav button { position: relative; }
    .metrics { grid-template-columns: repeat(2, 1fr); }
    .page { padding: 18px 16px 48px; }
  }
</style>
</head>
<body>

<!-- 图标描边的渐变：面板里所有 .ring-btn 的线性图标共用这一份 defs。
     图标上的 stroke 属性引用它，引不到会静默变成 none（图标整个消失），所以这段别删。
     色值必须跟 .ring-btn 的底环一致（主题那套紫→青），否则环和图标会对不上色。
     ⚠️ 想在本文件里数这个引用出现了几次时记住：当前有 3 处，就是下面那三个 <svg>。 -->
<svg width="0" height="0" style="position:absolute;pointer-events:none" aria-hidden="true" focusable="false">
  <defs>
    <linearGradient id="icoGrad" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#7c3aed"/>
      <stop offset=".48" stop-color="#6366f1"/>
      <stop offset="1" stop-color="#06b6d4"/>
    </linearGradient>
  </defs>
</svg>

<div class="blob b1"></div>
<div class="blob b2"></div>
<div class="blob b3"></div>

<div id="gate" class="overlay">
  <div class="box glass">
    <h2>登录面板</h2>
    <p>默认账号 <code>admin</code>，密码 <code>password</code> —— 登录后请尽快修改。</p>
    <div class="field">
      <label>用户名</label>
      <input type="text" id="userName" autocomplete="username" value="admin">
    </div>
    <div class="field">
      <label>密码</label>
      <input type="password" id="passWord" autocomplete="current-password">
    </div>
    <p class="err" id="gateErr"></p>
    <div class="row-end" style="margin-top:8px">
      <button class="primary" id="gateBtn">登录</button>
    </div>
  </div>
</div>

<div id="pwdGate" class="overlay hidden">
  <div class="box glass">
    <h2>修改面板密码</h2>
    <p>至少 6 位。改完其他设备上的登录会一并失效，需要重新登录。</p>
    <div class="field">
      <label>当前密码</label>
      <input type="password" id="pwdOld" autocomplete="current-password">
    </div>
    <div class="field">
      <label>新密码</label>
      <input type="password" id="pwdNew" autocomplete="new-password">
    </div>
    <div class="field">
      <label>确认新密码</label>
      <input type="password" id="pwdNew2" autocomplete="new-password">
    </div>
    <p class="err" id="pwdErr"></p>
    <div class="row-end">
      <button id="pwdCancel">取消</button>
      <button class="primary" id="pwdOk">确定</button>
    </div>
  </div>
</div>

<div id="batchGate" class="overlay hidden">
  <div class="box glass" style="width:min(560px,100%)">
    <h2>批量解析</h2>
    <p>一行一条 av / BV 链接，逐条解析，结果并进同一个列表。<b>会替换当前列表</b>（与单条解析一样，只是这一次把多条链接合在一起）。<span id="batchCount">已填 0 条</span></p>
    <textarea id="batchText" class="batchbox" rows="10" spellcheck="false" placeholder="https://www.bilibili.com/video/BV1xx411c7mD&#10;BV1xx411c7mD&#10;一次最多 50 条"></textarea>
    <label class="batchauto"><input type="checkbox" id="batchAuto"> 每条解析完自动加入下载列表</label>
    <p class="err" id="batchErr"></p>
    <div class="row-end">
      <button id="batchCancel">取消</button>
      <button class="primary" id="batchOk">开始解析</button>
    </div>
  </div>
</div>

<div id="histGate" class="overlay hidden">
  <div class="box glass" style="width:min(760px,100%)">
    <h2>解析记录</h2>
    <p>只保留最近 100 条，按解析时间从新到旧 · <span id="histCount">—</span></p>
    <div id="histList" class="histlist"></div>
    <p class="err" id="histErr"></p>
    <div class="row-end">
      <button id="histClear">清除历史</button>
      <button class="primary" id="histClose">关闭</button>
    </div>
  </div>
</div>

<aside class="side glass">
  <div class="brand">
    <span class="mark"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17.813 4.653h.854q2.266.08 3.773 1.574Q23.946 7.72 24 9.987v7.36q-.054 2.266-1.56 3.773c-1.506 1.507-2.262 1.524-3.773 1.56H5.333q-2.266-.054-3.773-1.56C.053 19.614.036 18.858 0 17.347v-7.36q.054-2.267 1.56-3.76t3.773-1.574h.774l-1.174-1.12a1.23 1.23 0 0 1-.373-.906q0-.534.373-.907l.027-.027q.4-.373.92-.373t.92.373L9.653 4.44q.107.106.187.213h4.267a.8.8 0 0 1 .16-.213l2.853-2.747q.4-.373.92-.373c.347 0 .662.151.929.4s.391.551.391.907q0 .532-.373.906zM5.333 7.24q-1.12.027-1.88.773q-.76.748-.786 1.894v7.52q.026 1.146.786 1.893t1.88.773h13.334q1.12-.026 1.88-.773t.786-1.893v-7.52q-.026-1.147-.786-1.894t-1.88-.773zM8 11.107q.56 0 .933.373q.375.374.4.96v1.173q-.025.586-.4.96q-.373.375-.933.374c-.56-.001-.684-.125-.933-.374q-.375-.373-.4-.96V12.44q0-.56.386-.947q.387-.386.947-.386m8 0q.56 0 .933.373q.375.374.4.96v1.173q-.025.586-.4.96q-.373.375-.933.374c-.56-.001-.684-.125-.933-.374q-.375-.373-.4-.96V12.44q.025-.586.4-.96q.373-.373.933-.373"/></svg></span>
    <div><b>Bili23 Downloader</b><small>NAS · WEB PANEL</small></div>
  </div>
  <nav class="nav">
    <button data-page="overview" class="active">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><rect x="1.8" y="1.8" width="5" height="5" rx="1.2"/><rect x="9.2" y="1.8" width="5" height="5" rx="1.2"/><rect x="1.8" y="9.2" width="5" height="5" rx="1.2"/><rect x="9.2" y="9.2" width="5" height="5" rx="1.2"/></svg>
      <span>概览</span>
    </button>
    <button data-page="downloading">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2.2v7.6m0 0L5 6.8m3 3l3-3M2.6 13.4h10.8"/></svg>
      <span>下载中</span><b class="nbadge zero" id="badgeActive">0</b>
    </button>
    <button data-page="done">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6.3"/><path d="M5.3 8.2l1.9 1.9 3.5-4"/></svg>
      <span>已完成</span><b class="nbadge zero" id="badgeDone">0</b>
    </button>
    <button data-page="fav">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.9l1.84 3.73 4.12.6-2.98 2.9.7 4.1L8 11.28l-3.68 1.95.7-4.1-2.98-2.9 4.12-.6z"/></svg>
      <span>收藏</span>
    </button>
    <button data-page="log">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="1.8" width="10" height="12.4" rx="1.5"/><path d="M5.5 5.2h5M5.5 8h5M5.5 10.8h3"/></svg>
      <span>日志</span>
    </button>
    <button data-page="naming">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M2.4 7.4V2.8a.4.4 0 0 1 .4-.4h4.6a1 1 0 0 1 .7.3l5.6 5.6a1 1 0 0 1 0 1.4l-4.2 4.2a1 1 0 0 1-1.4 0L2.7 8.1a1 1 0 0 1-.3-.7z"/><circle cx="5.3" cy="5.3" r="1"/></svg>
      <span>命名规则</span>
    </button>
    <button data-page="identify">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M2.4 3.9h8.2M2.4 7.1h5.4M2.4 10.3h3.6"/><circle cx="10.9" cy="10.3" r="2.6"/><path d="M12.9 12.3l1.9 1.9"/></svg>
      <span>名称识别</span>
    </button>
    <button data-page="sync">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M4.6 12.4a2.9 2.9 0 1 1 .4-5.76 3.9 3.9 0 0 1 7.4 1.19 2.4 2.4 0 0 1-.5 4.57z"/><path d="M6.4 14.2l1.6-1.6 1.6 1.6"/></svg>
      <span>云端同步</span>
    </button>
    <button data-page="mcp">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2.2" width="12" height="4.6" rx="1.2"/><rect x="2" y="9.2" width="12" height="4.6" rx="1.2"/><path d="M4.6 4.5h.01M4.6 11.5h.01M7 4.5h2.4M7 11.5h2.4"/></svg>
      <span>MCP 服务器</span>
    </button>
    <button data-page="settings">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M2 4.6h6.2M11.4 4.6H14M2 11.4h2.6M8 11.4h6"/><circle cx="9.6" cy="4.6" r="1.8"/><circle cx="5.4" cy="11.4" r="1.8"/></svg>
      <span>设置</span>
    </button>
    <button data-page="account">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6.3"/><circle cx="8" cy="6.4" r="2"/><path d="M4 12.4c.8-1.8 2.2-2.7 4-2.7s3.2.9 4 2.7"/></svg>
      <span>B站账号</span>
    </button>
    <button data-page="notify">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12.4 6.6a4.4 4.4 0 0 0-8.8 0c0 3.2-1.3 4.3-1.3 4.3h11.4s-1.3-1.1-1.3-4.3"/><path d="M9.4 13.1a1.6 1.6 0 0 1-2.8 0"/></svg>
      <span>通知</span>
    </button>
  </nav>
  <div class="me">
    <i class="dot" id="loginDot"></i><span id="loginText">检查中</span>
    <button class="ico-btn" id="outBtn" title="退出登录">⎋</button>
  </div>
</aside>

<main>

<!-- 概览 -->
<section class="page on" id="page-overview">
  <header class="phead">
    <h1>概览</h1>
    <div class="pacts">
      <button class="ring-btn" id="refreshBtn" title="刷新数据" aria-label="刷新数据">
        <svg viewBox="0 0 24 24" fill="none" stroke="url(#icoGrad)" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <path d="M20.49 15a9 9 0 1 1-2.12-9.36L22.5 9.5"/>
          <polyline points="22.5 4 22.5 9.5 17 9.5"/>
        </svg>
      </button>
      <button class="ring-btn" id="ovBackupBtn" title="云端备份：等下载任务全部完成后自动同步到 115 网盘" aria-label="云端备份">
        <svg viewBox="0 0 24 24" fill="none" stroke="url(#icoGrad)" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 13v8"/>
          <path d="M4 14.9A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.24"/>
          <path d="m8 17 4-4 4 4"/>
        </svg>
      </button>
      <button class="ring-btn" id="ovPwdBtn" title="修改面板密码" aria-label="修改面板密码">
        <svg viewBox="0 0 24 24" fill="none" stroke="url(#icoGrad)" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <rect width="18" height="11" x="3" y="11" rx="2" ry="2"/>
          <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
        </svg>
      </button>
    </div>
  </header>

  <div class="glass hero">
    <h1>粘贴链接，开始下载</h1>
    <p>支持 BV 号 / ep / ss / 收藏夹 · 自动按「剧名 / 季 / 集」命名归档，尾部画质与音质以实读为准</p>
    <div class="add">
      <input type="text" id="url" placeholder="https://www.bilibili.com/video/BV… 粘贴后回车即可解析" autocomplete="off">
      <button class="primary" id="parseBtn">解析</button>
      <!-- 与桌面端解析页那两颗入口对应：批量解析在「解析」的分裂下拉里，
           解析记录是列表上方的时钟图标。面板省掉下拉，两颗并排摆在这儿 -->
      <button id="batchBtn" title="一次粘贴多条 av / BV 链接，逐条解析后并进同一个列表">批量解析</button>
      <button id="histBtn" title="最近解析过的链接，可点一条重新解析">解析记录</button>
    </div>
    <div class="metrics">
      <div class="glass met"><span class="lab">活动任务</span><b id="tileActive">0</b><small class="dim" id="metActiveSub">加载中</small></div>
      <div class="glass met"><span class="lab">已完成</span><b id="tileDone">0</b><small class="dim">累计入库</small></div>
      <div class="glass met"><span class="lab">实时速度</span><b id="metSpeed">0<small class="unit"> MB/s</small></b><small class="dim" id="metSpeedSub">—</small></div>
      <div class="glass met"><span class="lab">下载目录</span><b id="ovPath" style="font-size:15px" title="">—</b><small class="dim">容器内挂载 /downloads</small><small id="ovFree" title="">剩余空间 —</small></div>
    </div>
  </div>

  <div id="parseMsg" class="err" style="margin:12px 4px 0"></div>
  <div id="parseList" class="hidden">
    <!-- 解析结果的头行。左右**不留内缩**（原来是 4px），这样「开始下载」的右缘
         与上下两张卡片的右缘落在同一条竖线上，不会看着差半个像素 -->
    <div class="row" style="margin:10px 0 8px">
      <span class="cnt" id="parseCount"></span>
      <!-- 同一系列还有别的季时才出现（番剧才有）。换一项就重新解析那一部，
           地址栏跟着改 —— 与桌面端解析页左下角那个下拉同一个用途 -->
      <select id="seasonSel" class="favseason hidden" title="同一系列的其它季"></select>
      <span class="spacer"></span>
      <button id="allBtn">全选</button>
      <!-- 🔴 这一行摆过两颗「下载中 / 已完成」跳转按钮（id 为 goActiveBtn 与 goDoneBtn，
           内侧计数 goActiveCnt / goDoneCnt）。2026-10-03 当天加上、当天被用户撤掉
           （原话「取消这里下载中，已完成，按钮」）。概览页现在**没有任何队列入口**，
           队列只在侧栏那两页 —— 别再回加：测试与线上验收都是「一个都不许出现」的双向断言。
           ⚠️ 真要加回来，三处联动缺一不可（HTML + refresh 里的计数赋值 + onclick 绑定）：
           只删其中一处就是每 3 秒 null 崩一次，而页面上只表现为"不刷新" -->
      <button class="primary" id="downBtn">开始下载</button>
    </div>
    <div class="glass panel" id="episodes" style="margin-top:0"></div>
  </div>
</section>

<!-- 下载中 -->
<section class="page" id="page-downloading">
  <header class="phead">
    <h1>下载中</h1>
    <span class="count" id="dlCount"></span>
    <div class="pacts">
      <span class="muted" style="font-size:12px" id="dlHintDetail"></span>
      <button class="sm" id="dlPauseAll">一键暂停</button>
      <button class="sm" id="dlCancelAll">一键取消</button>
      <button class="sm" id="dlStartAll">一键开始</button>
    </div>
  </header>
  <div class="glass panel" style="margin-top:0">
    <div class="h">任务列表<span class="sub">每 3 秒自动刷新</span></div>
    <div id="activeDetail"></div>
  </div>
</section>

<!-- 已完成 -->
<section class="page" id="page-done">
  <header class="phead">
    <h1>已完成</h1>
    <span class="count" id="doneCount"></span>
    <div class="pacts">
      <button class="sm danger" id="doneClearBtn">清理记录</button>
    </div>
  </header>
  <div class="glass panel" style="margin-top:0">
    <div class="h">最近完成<span class="sub" id="donePageHint">按完成时间倒序</span></div>
    <div id="done"></div>
    <div class="pager hidden" id="donePager"></div>
    <p class="muted" style="margin:10px 0 0">
      「清理记录」只清空这个列表，<b>已下载的文件不会被删除</b>；
      记录删掉后同名资源可再次下载。
    </p>
  </div>
</section>

<!-- 账号收藏 -->
<section class="page" id="page-fav">
  <header class="phead">
    <h1>账号收藏</h1>
    <span class="count" id="favCount"></span>
    <div class="pacts">
      <button class="sm" id="favReloadBtn">刷新</button>
    </div>
  </header>
  <div class="glass panel" style="margin-top:0">
    <div class="favtabs" id="favTabs">
      <button data-fav="favorite" class="active">收藏夹</button>
      <button data-fav="subscription">订阅合集</button>
      <button data-fav="follow">追番追剧</button>
      <button data-fav="watch_later">稍后再看</button>
      <button data-fav="history">历史记录</button>
    </div>
    <div class="favgrid" id="favList"><div class="empty">点上面分类加载</div></div>
    <p class="muted" style="margin:0 18px 14px">
      前三个分类点开卡片看内容；<b>「稍后再看」「历史记录」点一下就直接解析出条目</b> ——
      与桌面端一致（它们本来就没有列表接口）。勾选后走的是「下载中」页那条解析链路。
    </p>
  </div>
</section>

<!-- 日志 -->
<section class="page" id="page-log">
  <header class="phead"><h1>运行日志</h1></header>
  <div class="logbar">
    <select id="logFile"></select>
    <select id="logLevel">
      <option value="all">全部级别</option>
      <option value="info">INFO</option>
      <option value="warn">WARNING</option>
      <option value="error">ERROR</option>
    </select>
    <select id="logLines">
      <option value="100">100 行</option>
      <option value="300" selected>300 行</option>
      <option value="1000">1000 行</option>
      <option value="2000">2000 行</option>
    </select>
    <label class="chk"><input type="checkbox" id="logAuto"> 自动刷新</label>
    <span class="spacer"></span>
    <button class="sm" id="logBtn">刷新</button>
  </div>
  <pre class="logbox" id="logBox">加载中…</pre>
  <p class="muted" id="logMeta" style="margin:8px 0 0"></p>
  <p class="muted" style="margin:14px 0 0">
    级别过滤按 app.log 的行格式判定，crash.log 的原始转储只在「全部级别」下显示。
  </p>
</section>

<!-- 命名规则 -->
<section class="page" id="page-naming">
  <header class="phead">
    <h1>命名规则</h1>
    <span class="count" id="nmCount"></span>
    <div class="pacts">
      <button class="sm danger" id="nmResetAllBtn">恢复全部默认</button>
      <button class="sm" id="nmReloadBtn">重新载入</button>
      <button class="sm primary" id="nmSaveBtn">保存</button>
    </div>
  </header>

  <div class="glass panel" style="margin-top:0;padding-bottom:16px">
    <div class="h">文件命名规则<span class="sub">保存后对之后新建的下载任务生效</span></div>

    <div class="nmwrap">
      <div class="nmlist">
        <div class="nmbar">
          <input type="text" id="nmSearch" placeholder="搜索规则…" autocomplete="off">
          <button class="sm" id="nmAddBtn" title="新建一条规则">＋</button>
          <button class="sm" id="nmDupBtn" title="复制选中的规则">⧉</button>
          <button class="sm danger" id="nmDelBtn" title="删除选中的规则">✕</button>
        </div>
        <div class="nmitems" id="nmItems"><div class="empty">加载中…</div></div>
      </div>

      <div class="nmedit" id="nmEdit">
        <div class="nmrow">
          <div class="nmf">
            <label>规则名称</label>
            <input type="text" id="nmName" autocomplete="off" spellcheck="false">
          </div>
          <div class="nmf">
            <label>适用类型</label>
            <select id="nmType"></select>
          </div>
        </div>

        <label class="chk"><input type="checkbox" id="nmDef"> 设为该类型的默认规则</label>

        <div class="nmf nmfull">
          <label>命名规则</label>
          <textarea id="nmRule" rows="3" spellcheck="false" placeholder="{leaf_title}"></textarea>
          <div class="hint">
            用 {变量} 取字段；&lt;…&gt; 是可选段，段内变量取不到值时整段连同前后缀一起丢弃（多P与单P共用一条规则时用它）
          </div>
        </div>

        <div class="nmf nmfull">
          <label>可用变量<span class="nmvlabel" id="nmVarHint"></span></label>
          <div class="nvars" id="nmVars"></div>
        </div>

        <p class="err" id="nmErr" style="margin:0"></p>

        <div class="nmf nmfull">
          <label>预览</label>
          <div class="nmpv" id="nmPreview"></div>
          <div class="nmmixed" id="nmMixed"></div>
        </div>

        <div class="cfgfoot">
          <button class="sm" id="nmRestoreBtn">恢复这条的内置默认</button>
          <span class="msg" id="nmNote"></span>
        </div>
      </div>
    </div>
  </div>
</section>

<!-- 名称识别 -->
<section class="page" id="page-identify">
  <header class="phead">
    <h1>名称识别</h1>
    <span class="count" id="alCount"></span>
    <div class="pacts">
      <button class="sm" id="alReloadBtn">重新载入</button>
      <button class="sm primary" id="alSaveBtn">保存</button>
    </div>
  </header>

  <div class="glass panel" style="margin-top:0;padding-bottom:16px">
    <div class="h">识别规则<span class="sub">自上而下匹配，先命中的生效 —— 更具体的规则请排在上面</span></div>

    <div class="nmwrap">
      <div class="nmlist">
        <div class="nmbar">
          <input type="text" id="alSearch" placeholder="搜索匹配内容…" autocomplete="off">
          <button class="sm" id="alAddBtn" title="新建一条识别规则">＋</button>
          <button class="sm" id="alDupBtn" title="复制选中的规则">⧉</button>
          <button class="sm danger" id="alDelBtn" title="删除选中的规则">✕</button>
        </div>
        <div class="nmbar">
          <button class="sm" id="alUpBtn" title="上移，优先级更高">↑ 上移</button>
          <button class="sm" id="alDownBtn" title="下移，优先级更低">↓ 下移</button>
        </div>
        <div class="nmitems" id="alItems"><div class="empty">加载中…</div></div>
      </div>

      <div class="nmedit" id="alEdit">
        <div class="nmrow">
          <div class="nmf">
            <label>匹配位置</label>
            <select id="alField"></select>
          </div>
          <div class="nmf">
            <label>匹配方式</label>
            <select id="alMode"></select>
          </div>
          <div class="nmf">
            <label>匹配内容</label>
            <input type="text" id="alMatch" autocomplete="off" spellcheck="false" placeholder="西游记续集">
          </div>
        </div>

        <div class="nmrow">
          <div class="nmf nmfull">
            <label>TMDB 链接<span class="idopt">自动填入匹配内容 / 剧名 / 季号 / 年份 / 编号</span></label>
            <div class="nminp">
              <input type="text" id="alTmdbUrl" autocomplete="off" spellcheck="false" placeholder="https://www.themoviedb.org/tv/62591">
              <button class="sm" id="alTmdbFill">获取</button>
            </div>
            <span class="hint" id="alTmdbMsg">匹配内容默认取 TMDB 名称 —— 对不上 B站那边的季标题时改一下</span>
          </div>
        </div>

        <div class="nmrow">
          <div class="nmf">
            <label>归并后的剧名<span class="idopt">改 {season_title}</span></label>
            <input type="text" id="alTitle" autocomplete="off" placeholder="西游记（留空则不改名）">
          </div>
          <div class="nmf">
            <label>季号<span class="idopt">改 {season_number}</span></label>
            <input type="text" id="alSeason" autocomplete="off" placeholder="2（留空则不改）">
          </div>
        </div>

        <div class="nmrow">
          <div class="nmf">
            <label>年份<span class="idopt">{year}</span></label>
            <input type="text" id="alYear" autocomplete="off" placeholder="1986">
          </div>
          <div class="nmf">
            <label>TMDB 编号<span class="idopt">{tmdb_id}</span></label>
            <input type="text" id="alTmdb" autocomplete="off" placeholder="13923">
          </div>
          <div class="nmf">
            <label>备注<span class="idopt">仅自己看</span></label>
            <input type="text" id="alNoteIn" autocomplete="off" placeholder="可选">
          </div>
        </div>

        <label class="chk"><input type="checkbox" id="alEnabled"> 启用这条规则</label>

        <p class="err" id="alErr" style="margin:0"></p>

        <div class="nmf nmfull">
          <label>预览<span class="idopt">取影视类型的样本，假定 B站给出的标题就是上面的匹配内容</span></label>
          <div class="idpv" id="alPreview"></div>
          <div class="nmmixed" id="alTip"></div>
        </div>

        <div class="cfgfoot">
          <span class="msg" id="alMsg"></span>
        </div>
      </div>
    </div>
  </div>
</section>

<!-- 云端同步 -->
<section class="page" id="page-sync">
  <header class="phead">
    <h1>云端同步</h1>
    <div class="pacts">
      <button class="sm" id="syncCancelBtn" hidden>取消等待</button>
      <button class="primary" id="syncBtn">☁ 排期备份</button>
    </div>
  </header>

  <div class="glass panel" style="margin-top:0">
    <div class="h">CloudDrive2<span class="sub">面板直连 CD2，触发它自己那条备份任务</span></div>
    <div style="padding:6px 20px 16px">
      <div class="field">
        <label>CD2 地址</label>
        <input type="text" id="syncHost" autocomplete="off" spellcheck="false" placeholder="192.168.3.36:19798">
        <div class="hint">CloudDrive2 的地址与端口，端口默认 19798</div>
      </div>
      <div class="field">
        <label>备份源目录</label>
        <input type="text" id="syncSource" autocomplete="off" spellcheck="false" placeholder="/Storage/哔哩哔哩">
        <div class="hint">CD2 内视角的绝对路径，要与 CD2 里那条备份任务的源完全一致，面板按它点名触发</div>
      </div>
      <div class="field">
        <label>CD2 账号</label>
        <input type="text" id="syncUser" autocomplete="off" spellcheck="false">
      </div>
      <div class="field">
        <label>CD2 密码</label>
        <input type="password" id="syncPass" autocomplete="new-password">
        <div class="hint">留空表示不修改。凭据只落在容器内的 sync.json（权限 0600），不会回显到页面</div>
      </div>
      <p class="err" id="syncErr" style="margin:4px 0 10px"></p>
      <div class="cfgfoot">
        <button class="sm" id="syncReloadBtn">重新载入</button>
        <button class="sm primary" id="syncSaveBtn">保存配置</button>
        <span class="msg" id="syncNote"></span>
      </div>
    </div>

    <div class="howto-in" id="syncState"></div>

    <div class="howto-in" id="syncPending"></div>

    <div class="howto-in">
      <b style="color:var(--text)">备份原理：</b>点「排期备份」后面板不立刻动手 —— 先等下载队列里
      所有任务都跑完，再等设置里配的延迟（默认 5 分钟，在设置页「云端同步」组里改），
      那几分钟正好留给 FFmpeg 合并与文件重命名落定，免得把半成品传上去。到点后用上面的账号密码
      登录 <code>CloudDrive2</code>（GetToken 换 JWT），按源目录找到它自己那条备份任务，让它
      重扫一遍（BackupRestartWalkingThrough）。增量对比、冲突策略、失败重试与上传进度都归
      CD2 的备份引擎管 —— 不再是在容器里 <code>cp</code> 一份、让每个文件都变成一条传输任务。
    </div>
  </div>
</section>

<!-- MCP 服务器 -->
<section class="page" id="page-mcp">
  <header class="phead">
    <h1>MCP 服务器</h1>
    <div class="pacts">
      <button class="sm" id="mcpReloadBtn">重新载入</button>
      <button class="sm primary" id="mcpSaveBtn">保存</button>
    </div>
  </header>
  <div class="glass panel" style="margin-top:0">
    <div class="h">MCP 服务器<span class="sub">让 AI 客户端接入下载器</span></div>
    <div style="padding:6px 20px 8px">
      <div id="mcpBox"><div class="empty">加载中…</div></div>
      <p class="err" id="mcpErr" style="margin:10px 0 6px"></p>
    </div>

    <div class="howto-in">
      <b style="color:var(--text)">MCP 是什么：</b>Model Context Protocol，AI 客户端（如 Claude、
      Cursor 等）通过它直接调用下载器的解析与任务管理能力。把上面的客户端配置复制进
      AI 客户端的 MCP 设置即可接入；访问令牌相当于这扇门的钥匙，请妥善保管。
    </div>
  </div>
</section>

<!-- 通知 -->
<section class="page" id="page-notify">
  <header class="phead">
    <h1>通知</h1>
    <div class="pacts">
      <button class="sm" id="ntReloadBtn">重新载入</button>
      <button class="sm primary" id="ntSaveBtn">保存</button>
    </div>
  </header>

  <p class="err" id="ntErr" style="margin:0 0 12px"></p>

  <div class="glass panel nt-acc" style="margin-top:0">
    <div class="nt-item collapsed" data-nt="wecom">
      <div class="h">
        <span class="nt-t"><b>企业微信应用</b><small>自建应用，发给指定成员</small></span>
        <span class="ntbadge" id="ntWecomBadge">未启用</span>
        <i class="fchev">▾</i>
      </div>

    <div style="padding:6px 20px 16px">
      <div class="setrow">
        <div>
          <div class="setname">启用企业微信通知</div>
          <div class="sethint">下载完成或失败时推送给下面的接收人</div>
        </div>
        <div class="setctl"><input type="checkbox" id="ntWecomOn"></div>
      </div>
      <div class="field" style="margin-top:14px">
        <label>企业 ID</label>
        <input type="text" id="ntWecomCorp" autocomplete="off" spellcheck="false" placeholder="ww182aa9502bd5aff3">
        <div class="hint">企业微信管理后台 → 我的企业 → 企业信息，最下面那条「企业 ID」</div>
      </div>
      <div class="field">
        <label>应用 AgentId</label>
        <input type="text" id="ntWecomAgent" autocomplete="off" spellcheck="false" placeholder="1000005">
        <div class="hint">应用管理 → 自建 → 点进那个应用，标题下面那个数字</div>
      </div>
      <div class="field">
        <label>应用 Secret</label>
        <div class="ntsecret">
          <input type="password" id="ntWecomSecret" autocomplete="off" spellcheck="false"
                 placeholder="点「查看」后复制的那一串">
          <button type="button" class="sm" id="ntWecomEye">显示</button>
        </div>
        <div class="hint">同一个应用详情页里的 Secret，点「查看」并复制</div>
      </div>
      <div class="field">
        <label>指定接收人</label>
        <input type="text" id="ntWecomUser" autocomplete="off" spellcheck="false" placeholder="zhangsan,lisi 或 @all">
        <div class="hint">填<b>成员账号</b>（不是姓名），多个用英文逗号隔开；填 <code>@all</code> 发给全企业</div>
      </div>
      <div class="field">
        <label>消息代理地址</label>
        <input type="text" id="ntWecomBase" autocomplete="off" spellcheck="false" placeholder="http://47.117.88.76:3100">
        <div class="hint">留空则直连企业微信官方地址。填了就把请求交给它中转 —— 用于固定出口 IPv4（企业微信要求调用方 IP 可信）<br>
          若发送时报 <code>60020 not allow to access from your ip</code>：企微只放行「企业可信 IP」名单里的出口地址，
          去应用详情页最下面把上面填的这台中转机的 IP（例如 <code>47.117.88.76</code>）加进去，再回来点发送测试</div>
      </div>
      <div class="cfgfoot">
        <button class="sm" id="ntWecomTestBtn">发送测试</button>
        <span class="msg" id="ntWecomTestMsg"></span>
      </div>
    </div>
    <div class="howto-in">
      用<b>自建应用</b>而不是群机器人：机器人只能发到它所在的那个群，地址里的 <code>key</code>
      拿到就永久有效；应用凭据随时可以吊销，也能点名发给指定成员。<br>
      Secret、Token 这类凭据<b>不会回显到页面上</b>（服务端不下发，导出配置时也会掩码）。
      输入框留空表示"这一项不动"，想换凭据就直接填新的覆盖。
    </div>
    </div>

    <div class="nt-item collapsed" data-nt="callback">
      <div class="h">
        <span class="nt-t"><b>企业微信回调</b><small>接收消息与事件，方向相反的那一半</small></span>
        <span class="ntbadge" id="ntCbBadge">未启用</span>
        <i class="fchev">▾</i>
      </div>

    <div style="padding:6px 20px 16px">
      <div class="setrow">
        <div>
          <div class="setname">启用回调</div>
          <div class="sethint">让企业微信把消息推给本面板；需要面板能从公网访问</div>
        </div>
        <div class="setctl"><input type="checkbox" id="ntCbOn"></div>
      </div>
      <div class="field" style="margin-top:14px">
        <label>回调 Token</label>
        <input type="text" id="ntCbToken" autocomplete="off" spellcheck="false" placeholder="未填写">
        <div class="hint">企微后台「接收消息服务器配置」里你自己填的那个 Token，两边必须一字不差</div>
      </div>
      <div class="field">
        <label>EncodingAESKey</label>
        <div class="ntsecret">
          <input type="password" id="ntCbAes" autocomplete="off" spellcheck="false"
                 placeholder="43 位字母数字">
          <button type="button" class="sm" id="ntCbEye">显示</button>
        </div>
        <div class="hint">同一处点「随机获取」得到的那串 43 位字符，原样复制过来</div>
      </div>
      <div class="field">
        <label>对外访问地址</label>
        <input type="text" id="ntCbBase" autocomplete="off" spellcheck="false" placeholder="https://bili23.892639.xyz:2662">
        <div class="hint">面板在外网的那个地址。<b>必须是域名或公网 IP + 端口</b>，留空则用你当前打开面板的地址（从局域网 IP 打开面板时，那个地址填进企微后台是不通的）</div>
      </div>
      <div class="field">
        <label>回调地址<span class="sub" style="margin-left:6px">填到企微后台的 URL</span></label>
        <div class="ntsecret">
          <input type="text" id="ntCbUrl" readonly placeholder="填好上面几项后自动生成">
          <button type="button" class="sm" id="ntCbCopy">复制</button>
        </div>
        <div class="hint">企微后台 → 应用管理 → 自建 → 点进应用 → 接收消息 → 设置 API 接收，把这一整条填进 URL 并保存</div>
      </div>
      <div class="cfgfoot">
        <span class="msg" id="ntCbMsg"></span>
      </div>
    </div>
    <div class="howto-in">
      与上面那组是<b>两个方向</b>：上面是面板推给企业微信，这里是企业微信推给面板，所以要多一份 Token
      与 EncodingAESKey。<br>
      回调地址是<b>公开</b>的 —— 企微服务器不会带面板令牌来敲门，拦人的活由签名干：只有同时掌握
      Token 与 EncodingAESKey 才算得出合法签名。两个值都按凭据对待，导出的配置里会被掩码。<br>
      在企微后台点「保存」时它会立刻发一条验证请求，通了下面「回调记录」里就会出现一条
      <b>URL 验证通过</b> —— 那就是端到端真的通了。
    </div>
    </div>

    <div class="nt-item collapsed" data-nt="telegram">
      <div class="h">
        <span class="nt-t"><b>Telegram</b><small>Bot 私聊、群或频道</small></span>
        <span class="ntbadge" id="ntTgBadge">未启用</span>
        <i class="fchev">▾</i>
      </div>

    <div style="padding:6px 20px 16px">
      <div class="setrow">
        <div>
          <div class="setname">启用 Telegram 通知</div>
          <div class="sethint">需要 Bot Token 与 Chat ID，且容器能连上 Telegram</div>
        </div>
        <div class="setctl"><input type="checkbox" id="ntTgOn"></div>
      </div>
      <div class="field" style="margin-top:14px">
        <label>Bot Token</label>
        <div class="ntsecret">
          <input type="password" id="ntTgToken" autocomplete="off" spellcheck="false"
                 placeholder="123456789:AAH…">
          <button type="button" class="sm" id="ntTgEye">显示</button>
        </div>
        <div class="hint">在 Telegram 里找 <code>@BotFather</code>，新建机器人后拿到</div>
      </div>
      <div class="field">
        <label>Chat ID</label>
        <input type="text" id="ntTgChat" autocomplete="off" spellcheck="false" placeholder="123456789 或 -1001234567890">
        <div class="hint">私聊是数字，群是负数，频道可以写 <code>@频道名</code>；不确定就找 <code>@userinfobot</code> 问一句</div>
      </div>
      <div class="field">
        <label>网络代理</label>
        <input type="text" id="ntProxy" autocomplete="off" spellcheck="false" placeholder="http://192.168.3.25:7890">
        <div class="hint">HTTP 代理，两个渠道共用。Telegram 的服务器在境外，容器不走代理连不上；企业微信在国内，留空即可 —— 上面那个「消息代理地址」是另一回事</div>
      </div>
      <div class="cfgfoot">
        <button class="sm" id="ntTgTestBtn">发送测试</button>
        <span class="msg" id="ntTgTestMsg"></span>
      </div>
    </div>
    </div>

    <div class="nt-item collapsed" data-nt="trigger">
      <div class="h">
        <span class="nt-t"><b>触发时机</b><small>哪两种结果值得推一条</small></span>
        <span class="ntbadge" id="ntTrigBadge">都关闭</span>
        <i class="fchev">▾</i>
      </div>

    <div style="padding:6px 20px 16px">
      <div class="setrow">
        <div>
          <div class="setname">下载完成时通知</div>
          <div class="sethint">整个合集下完才推一条，不是每集一条</div>
        </div>
        <div class="setctl"><input type="checkbox" id="ntOnComplete"></div>
      </div>
      <div class="setrow">
        <div>
          <div class="setname">下载失败时通知</div>
          <div class="sethint">自动重试期间的失败不推，只有真正放弃时推一次</div>
        </div>
        <div class="setctl"><input type="checkbox" id="ntOnFail"></div>
      </div>
    </div>
    <div class="howto-in">
      完成通知要等两件事都成立才发：队列里没有任务在跑了，且已有 20 秒没有新的完成事件。
      所以西游记 1-25 集这种会汇总成一条，按合集列出剧名、年份与集数（第1-25集，共25集）。
      「年份」取自「名称识别」表，没配规则或没填年份时那一行不显示。暂停或失败的任务
      不算在跑，不会把通知一直压住。失败通知立即发送，但<b>自动重试排着队的不推</b>，
      与界面上那条报错保持同一个取舍。
    </div>
    </div>

    <div class="nt-item collapsed" data-nt="cbhistory">
      <div class="h">
        <span class="nt-t"><b>回调记录</b><small>企业微信推过来的验证与消息</small></span>
        <span class="ntbadge" id="ntCbHistBadge">暂无</span>
        <i class="fchev">▾</i>
      </div>

    <div id="ntCallbackHistory" style="padding:6px 20px 16px"></div>
    <div class="howto-in">
      企微后台点「保存」时那一下验证请求也会记在这里。看到 <b>URL 验证通过</b> 就说明
      外网地址、反代、Token 与 EncodingAESKey 全都对上了 —— 这条是从公网真打进来的一次，
      不是本地自测。
    </div>
    </div>

    <div class="nt-item collapsed" data-nt="history">
      <div class="h">
        <span class="nt-t"><b>最近发送记录</b><small>最多 20 条，重启后重新累计</small></span>
        <span class="ntbadge" id="ntHistBadge">暂无</span>
        <i class="fchev">▾</i>
      </div>

    <div id="ntHistory" style="padding:6px 20px 16px"></div>
    </div>
  </div>
</section>

<!-- 设置 -->
<section class="page" id="page-settings">
  <header class="phead">
    <h1>下载设置</h1>
    <div class="pacts">
      <button class="ico-btn" id="pwdBtn" title="修改面板密码">🔒</button>
      <button class="sm" id="setReloadBtn">重新载入</button>
      <button class="sm primary" id="setSaveBtn">保存</button>
    </div>
  </header>
  <div class="glass panel" style="margin-top:0">
    <div class="h">面板设置<span class="sub">保存后立即生效</span></div>
    <div style="padding:6px 20px 8px">
      <div id="setFields"><div class="empty">加载中…</div></div>
      <div id="setReadonly"></div>
      <div class="setgroup">配置文件设置</div>
      <div class="setrow">
        <div>
          <div class="setname">导入 / 导出 / 恢复默认</div>
          <div class="sethint">导出当前配置为 JSON；导入的配置逐项写入并立即生效；恢复默认会重置全部设置（包括面板账号密码）</div>
        </div>
        <div class="setctl">
          <button type="button" class="sm" id="cfgExportBtn">导出</button>
          <button type="button" class="sm" id="cfgImportBtn">导入</button>
          <button type="button" class="sm danger" id="cfgResetBtn">恢复默认</button>
          <input type="file" id="cfgFile" accept=".json,application/json" style="display:none">
        </div>
      </div>
      <p class="err" id="setErr" style="margin:10px 0 6px"></p>
    </div>
  </div>
</section>

<!-- B站账号 -->
<section class="page" id="page-account">
  <header class="phead"><h1>B站账号授权登录</h1></header>
  <div class="glass panel" style="margin-top:0">
    <div style="padding:16px 20px">
      <div class="scan">
        <div class="qr" id="qrBox"><div class="empty">点右侧按钮获取二维码</div></div>
        <div class="scan-info">
          <p id="qrStatus" class="muted">用哔哩哔哩手机客户端扫码，并在手机上确认。</p>
          <div class="row">
            <button class="primary" id="qrBtn">获取二维码</button>
            <button id="biliOutBtn" class="hidden">退出 B站登录</button>
          </div>
          <p class="muted" style="margin-top:12px">
            未登录也能下载，只是拿不到会员内容与高画质；登录信息保存在服务端配置里。
          </p>
        </div>
      </div>
    </div>
  </div>
</section>

</main>

<div id="toast"></div>

<script>
(function () {
  var episodes = [];
  var timer = null;
  var qrKey = "";
  var qrTimer = null;
  var logTimer = null;

  function $(id) { return document.getElementById(id); }

  function toast(msg) {
    var el = $("toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(el._t);
    el._t = setTimeout(function () { el.classList.remove("show"); }, 2600);
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  // 面板接口的 401 是"会话没了"，登录接口的 401 是"密码错"，
  // 前者要弹登录框，后者要让调用方把错误显示在表单里
  function isPublic(path) { return path === "api/panel/login"; }

  function post(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (r.status === 401 && !isPublic(path)) {
          needLogin();
          throw new Error("unauthorized");
        }

        if (!r.ok) throw new Error(data.error || ("请求失败（" + r.status + "）"));

        return data;
      });
    });
  }

  // GET 版：读设置与读日志在用。与 post 保持同一套 401 处理 ——
  // 会话过期时不管哪个方法都要弹回登录框
  function get(path) {
    return fetch(path, { method: "GET" }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (r.status === 401 && !isPublic(path)) {
          needLogin();
          throw new Error("unauthorized");
        }

        if (!r.ok) throw new Error(data.error || ("请求失败（" + r.status + "）"));

        return data;
      });
    });
  }

  // MCP 工具调用：返回值是 MCP 的结果结构，这里拆出结构化内容
  function api(tool, args) {
    return post("api/call", { tool: tool, arguments: args || {} }).then(function (data) {
      if (data.isError) {
        var text = (data.content && data.content[0] && data.content[0].text) || "调用失败";
        throw new Error(text);
      }

      return data.structuredContent || {};
    });
  }

  function needLogin() {
    if (timer) { clearInterval(timer); timer = null; }
    stopQr();
    stopLogAuto();

    $("gate").classList.remove("hidden");
    $("passWord").value = "";
    $("passWord").focus();
  }

  $("gateBtn").onclick = function () {
    var btn = this;
    var user = $("userName").value.trim();
    var pass = $("passWord").value;

    if (!user || !pass) { $("gateErr").textContent = "请填写用户名和密码"; return; }

    $("gateErr").textContent = "";
    btn.disabled = true;

    post("api/panel/login", { username: user, password: pass }).then(function () {
      $("gate").classList.add("hidden");
      boot();
    }).catch(function (e) {
      $("gateErr").textContent = e.message === "unauthorized" ? "" : e.message;
    }).then(function () {
      btn.disabled = false;
    });
  };

  $("passWord").addEventListener("keydown", function (e) {
    if (e.key === "Enter") $("gateBtn").click();
  });

  $("outBtn").onclick = function () {
    post("api/panel/logout").then(function () {
      location.reload();
    }).catch(function () {
      location.reload();
    });
  };

  // ---- 页切换 ----
  //
  // data-page 的值就是页面 section 的 id 后缀（page-<key>），没有中间映射 ——
  // 前一版浮层开关上踩过"另设一套短名再映射"的坑：两套命名一错位就是
  // "点了没反应"，而且异常发生在监听器里页面上不留痕迹。只留一套命名，
  // 取不到页面时明确报错
  //
  // 各页的数据是按需加载的：日志、CD2 配置只有切过去才请求，
  // B站二维码同理（用户没到账号页就不必向 B 站要二维码）

  function showPage(key) {
    var page = $("page-" + key);

    if (!page) { console.error("没有这个页面：" + key); return; }

    document.querySelectorAll(".nav button").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-page") === key);
    });
    document.querySelectorAll(".page").forEach(function (p) {
      p.classList.toggle("on", p === page);
    });

    // 离开日志页就停自动刷新，人在别的页没必要继续打请求
    if (key === "log") {
      loadLogs();
    } else {
      stopLogAuto();
    }

    if (key === "settings") loadSettings();
    if (key === "mcp") loadSettings();
    if (key === "sync") loadSyncConfig();
    // 命名规则只在第一次进来时拉：这一页有未保存的改动，每次切回来都重载
    // 会把用户正在写的规则冲掉（要重来一次有专门的「重新载入」按钮）
    if (key === "naming" && !nmState.loaded) loadNaming();
    if (key === "identify" && !alState.loaded) loadIdentify();
    // 通知页同理：输入框里可能正躺着一段填了一半的 Webhook，切回来重绘会冲掉。
    // 已经载入过就只刷发送记录，配置留给「重新载入」按钮
    if (key === "notify") loadNotify(ntState.loaded);
    // 收藏页第一次进来才拉数据（要新数据有「刷新」按钮）——收藏夹在手机上随时会变，
    // 但每切一次 tab 就打一次 B站接口没必要
    if (key === "fav" && !favState.kind) favSetKind("favorite");
    if (key === "account" && !qrKey && !$("qrBox").querySelector("svg")) startQr();
  }

  document.querySelectorAll(".nav button").forEach(function (b) {
    b.onclick = function () { showPage(b.getAttribute("data-page")); };
  });

  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;

    if (!$("pwdGate").classList.contains("hidden")) {
      $("pwdGate").classList.add("hidden");
      return;
    }

    showPage("overview");
  });

  // ---- 改密码 ----

  // 设置页那颗小方按钮与概览右上角那颗圆按钮共用：清空三格、弹出、聚焦旧密码
  function openPwdGate() {
    $("pwdOld").value = "";
    $("pwdNew").value = "";
    $("pwdNew2").value = "";
    $("pwdErr").textContent = "";
    $("pwdGate").classList.remove("hidden");
    $("pwdOld").focus();
  }

  $("pwdBtn").onclick = openPwdGate;
  $("ovPwdBtn").onclick = openPwdGate;

  $("pwdCancel").onclick = function () {
    $("pwdGate").classList.add("hidden");
  };

  $("pwdOk").onclick = function () {
    var oldPass = $("pwdOld").value;
    var newPass = $("pwdNew").value;

    if (newPass !== $("pwdNew2").value) {
      $("pwdErr").textContent = "两次输入的新密码不一致";
      return;
    }

    var btn = this;
    btn.disabled = true;
    $("pwdErr").textContent = "";

    post("api/panel/password", { current: oldPass, password: newPass }).then(function () {
      $("pwdGate").classList.add("hidden");
      toast("密码已修改");
    }).catch(function (e) {
      $("pwdErr").textContent = e.message;
    }).then(function () {
      btn.disabled = false;
    });
  };

  // ---- B站扫码登录 ----

  function stopQr() {
    if (qrTimer) { clearInterval(qrTimer); qrTimer = null; }
    qrKey = "";
  }

  function setQrStatus(text, cls) {
    var el = $("qrStatus");
    el.textContent = text;
    el.className = cls || "muted";
  }

  function startQr() {
    var btn = $("qrBtn");
    btn.disabled = true;
    setQrStatus("正在获取二维码…");

    post("api/panel/qr/start").then(function (d) {
      qrKey = d.qrcode_key;
      // 正常情况下服务端一定会给 svg；万一没给，宁可显示一句提示，
      // 也别把 undefined 印在二维码框里
      $("qrBox").innerHTML = d.svg || '<div class="empty">没有拿到二维码</div>';
      setQrStatus(d.svg ? "等待扫码…" : "二维码获取异常，请重试", d.svg ? "muted" : "err");

      stopQrTimerOnly();

      if (d.svg) qrTimer = setInterval(pollQr, 2000);
    }).catch(function (e) {
      if (e.message !== "unauthorized") setQrStatus(e.message, "err");
    }).then(function () {
      btn.disabled = false;
    });
  }

  function stopQrTimerOnly() {
    if (qrTimer) { clearInterval(qrTimer); qrTimer = null; }
  }

  var QR_TEXT = {
    86101: "等待扫码…",
    86090: "已扫码，请在手机上确认",
    86038: "二维码已过期，请重新获取"
  };

  function pollQr() {
    if (!qrKey) { stopQrTimerOnly(); return; }

    var key = qrKey;

    post("api/panel/qr/poll", { qrcode_key: key }).then(function (d) {
      // 轮询回来时可能已经换了二维码，旧结果直接丢掉
      if (key !== qrKey) return;

      if (d.code === 0) {
        stopQr();
        $("qrBox").innerHTML = '<div class="empty ok">登录成功</div>';
        setQrStatus("已登录 B站账号，高画质与会员内容已解锁。", "ok");
        refreshLogin();
        return;
      }

      setQrStatus(QR_TEXT[d.code] || ("状态 " + d.code), d.code === 86038 ? "warn" : "muted");

      if (d.code === 86038) {
        stopQr();
        $("qrBox").innerHTML = '<div class="empty">二维码已过期</div>';
      }
    }).catch(function (e) {
      if (e.message === "unauthorized") return;

      stopQr();
      setQrStatus(e.message, "err");
    });
  }

  $("qrBtn").onclick = function () {
    stopQr();
    $("qrBox").innerHTML = '<div class="empty">正在获取…</div>';
    startQr();
  };

  $("biliOutBtn").onclick = function () {
    var btn = this;
    btn.disabled = true;

    post("api/panel/bili/logout").then(function () {
      stopQr();
      $("qrBox").innerHTML = '<div class="empty">点「获取二维码」重新登录</div>';
      setQrStatus("已退出 B站登录。", "muted");
      toast("已退出 B站登录");
      refreshLogin();
    }).catch(function (e) {
      if (e.message !== "unauthorized") toast(e.message);
    }).then(function () {
      btn.disabled = false;
    });
  };

  // ---- 任务列表 ----

  function fmtSize(n) {
    if (!n) return "0 B";
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(i === 0 ? 0 : 1) + " " + u[i];
  }

  var STATUS_TEXT = {
    queued: "排队中", parsing: "解析中", downloading: "下载中", paused: "已暂停",
    completed: "已完成", ffmpeg_queued: "等待处理", merging: "合并中", converting: "转换中",
    additional_processing: "后处理", failed: "下载失败", ffmpeg_failed: "处理失败",
    invalid: "文件缺失"
  };

  // 状态对应的标签配色，以及哪些状态下该显示哪个操作按钮。
  // 合并 / 转换中的任务不能取消（后端也会拒），这里就不给按钮了
  var BUSY = ["queued", "parsing", "downloading"];
  var CANCELLABLE = ["queued", "parsing", "downloading", "paused", "ffmpeg_queued", "failed", "ffmpeg_failed"];

  function statusClass(s) {
    if (s === "paused") return "tag paused";
    if (s === "failed" || s === "ffmpeg_failed" || s === "invalid") return "tag failed";
    if (BUSY.indexOf(s) >= 0 || s === "merging" || s === "converting") return "tag running";
    return "tag";
  }

  function taskActions(t) {
    var s = String(t.status || "").toLowerCase();
    var id = esc(t.task_id);
    var out = "";

    if (s === "paused") {
      out += '<button class="sm" data-act="resume" data-id="' + id + '">继续</button>';
    } else if (BUSY.indexOf(s) >= 0) {
      out += '<button class="sm" data-act="pause" data-id="' + id + '">暂停</button>';
    }

    if (CANCELLABLE.indexOf(s) >= 0) {
      out += '<button class="sm" data-act="cancel" data-id="' + id + '">取消</button>';
    }

    return out ? '<div class="acts">' + out + "</div>" : "";
  }

  function renderTasks(box, tasks, isDone) {
    if (!tasks.length) {
      box.innerHTML = '<div class="empty">' + (isDone ? "暂无已完成任务" : "当前没有正在下载的任务") + "</div>";
      return;
    }

    box.innerHTML = tasks.map(function (t) {
      var pct = Math.max(0, Math.min(100, Number(t.progress) || 0));
      var raw = String(t.status || "").toLowerCase();
      var meta = [];

      if (t.total_size) meta.push(fmtSize(t.downloaded_size) + " / " + fmtSize(t.total_size));
      if (t.speed) meta.push(fmtSize(t.speed) + "/s");

      // 状态类挂在任务行上：光点颜色、进度条颜色都由它驱动
      return '<div class="task s-' + esc(raw || "idle") + (isDone ? " done" : "") + '">'
        + '<div class="head">'
        + '<span class="tdot"></span>'
        + '<span class="name" title="' + esc(t.title) + '">' + esc(t.title || "(无标题)") + "</span>"
        + '<span class="' + statusClass(raw) + '">' + esc(STATUS_TEXT[raw] || t.status) + "</span>"
        + '<span class="meta">' + meta.join(" · ") + "</span>"
        + (isDone ? "" : '<span class="pct">' + pct + "%</span>")
        + "</div>"
        + (isDone ? "" : '<div class="bar"><i style="width:' + pct + '%"></i></div>')
        + (isDone ? "" : taskActions(t))
        + "</div>";
    }).join("");
  }

  document.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest("[data-act]") : null;

    if (!btn) return;

    var act = btn.getAttribute("data-act");
    var id = btn.getAttribute("data-id");

    if (act === "cancel" && !confirm("取消这个任务？未完成的临时文件会被删除。")) return;

    btn.disabled = true;

    api(act + "_task", { task_id: id }).then(function () {
      toast({ pause: "已暂停", resume: "已继续", cancel: "已取消" }[act] || "已处理");
      return refresh();
    }).catch(function (err) {
      if (err.message !== "unauthorized") toast(err.message);
    }).then(function () {
      btn.disabled = false;
    });
  });

  // ---- 「一键暂停」/「一键取消」/「一键开始」 ----
  //
  // 作用于未完成列表里的**全部任务**：服务端逐条复用单任务那条控制链路
  //（含重启残留态的归一化、"QUEUED 只落状态不启动"这些特例），所以这里一次调用、
  // 不传 id。若改成前端循环调 30 次 pause_task，期间每 3 秒的自动刷新还会插进来，
  // 列表会一边暂停一边重画。
  //
  // ★「一键开始」走的是**批量排队**那条路，不是把暂停的都立刻续传：同时下几个由
  // 设置里的「同时下载任务数」决定，其余排队等补位。所以回执里除了 changed 还有
  // running / limit 两个数，toast 要如实说成"排队 N 个 / 正在下 M 个"，
  // 别写"已开始 N 个"——那会让人以为并发上限没生效
  //
  // ★「一键取消」是这三颗里唯一**不可逆**的：它会连未完成的临时文件一起删掉。
  // 所以确认框放在 bulkControl 的最前面（而不是点上就跑），并且要挡住"合并中
  // 取消不了"这条 —— 服务端对 MERGING/CONVERTING 会记成 skipped，toast 里要
  // 区分开，否则用户看到"取消了 42 个"却仍有任务在合并，会以为按钮失灵
  var bulkBusy = false;

  function bulkControl(action, doneText) {
    if (bulkBusy) return;

    if (action === "cancel"
        && !confirm("取消全部未完成的任务？\n\n"
                    + "未完成的临时文件会被删除，已经下载好的文件不受影响。\n"
                    + "这个操作不可撤销。")) return;

    bulkBusy = true;
    $("dlPauseAll").disabled = true;
    $("dlCancelAll").disabled = true;
    $("dlStartAll").disabled = true;

    api(action + "_all_tasks", {}).then(function (d) {
      var total = Number(d.total) || 0;
      var changed = Number(d.changed) || 0;

      if (!total) {
        toast("队列里没有未完成的任务");

        return refresh();
      }

      if (action === "resume") {
        var running = Number(d.running) || 0;
        var limit = Number(d.limit) || 0;

        // 已在下 / 已排队的本来就在列表里，changed 只算这次从暂停转成排队的那些
        toast("已排队 " + changed + " 个，正在下载 " + running + " 个（上限 " + limit + "）"
              + (changed < total ? "，其余无需处理" : ""));

        return refresh();
      }

      if (action === "cancel") {
        // skipped 的那几条是正在合并 / 转换的任务 —— 服务端有意不动它们（半成品
        // 删了就白跑）。不把这层说出来，用户会以为"取消了 38 个还剩 4 个没反应"
        toast("已取消 " + changed + " 个任务，未完成的临时文件已删除"
              + (changed < total
                 ? "；其余 " + (total - changed) + " 个正在合并或转换，完成后再处理"
                 : ""));

        return refresh();
      }

      toast("已" + doneText + " " + changed + " 个任务"
            + (changed < total ? "，其余无需处理" : ""));

      return refresh();
    }).catch(function (err) {
      if (err.message !== "unauthorized") toast(err.message);
    }).then(function () {
      // 不管成败都放开：按钮的真实可用状态由 refresh 按当前队列长度重算
      bulkBusy = false;
      refresh();
    });
  }

  $("dlPauseAll").onclick = function () { bulkControl("pause", "暂停"); };
  $("dlCancelAll").onclick = function () { bulkControl("cancel", "取消"); };
  $("dlStartAll").onclick = function () { bulkControl("resume", "开始"); };

  // 「清理记录」：只删数据库里的记录行，不动任何文件（后端 api_done_clear 同语义）。
  // 两个坑：
  //   ① 后端删表走写线程、异步落地 → 这里按"乐观清空 + 稍后回读"处理，
  //      否则用户会看到"点了没反应"
  //   ② 它是不可逆操作 → 确认框必须写明「文件不会被删除」，别让人以为是在删文件
  var doneClearBusy = false;

  // ---- 「已完成」列表的分页 ----
  //
  // 已完成是长期累积的，几百上千条全渲染既慢又没法看，所以按页取
  // （offset 由服务端下推到 SQL，不是全查回来再切）。页码是前端状态：
  // 自动刷新每 3 秒跑一次，翻页不能把用户弹回第一页
  var DONE_PAGE_SIZE = 20;
  var donePage = 1;
  var doneTotal = 0;

  function donePageCount() {
    return Math.max(1, Math.ceil(doneTotal / DONE_PAGE_SIZE));
  }

  function renderDonePager() {
    var box = $("donePager");
    var pages = donePageCount();

    // 只剩一页时整条藏掉：一个列表底下挂一行"第 1 / 1 页"纯属噪声
    if (doneTotal <= DONE_PAGE_SIZE) {
      box.classList.add("hidden");
      box.innerHTML = "";
      $("donePageHint").textContent = doneTotal
        ? "按完成时间倒序 · 共 " + doneTotal + " 条"
        : "按完成时间倒序";

      return;
    }

    var prev = donePage > 1;
    var next = donePage < pages;

    box.classList.remove("hidden");
    box.innerHTML =
      '<button class="sm" data-pg="-1"' + (prev ? "" : " disabled") + ">上一页</button>"
      + '<button class="sm" data-pg="1"' + (next ? "" : " disabled") + ">下一页</button>"
      + '<span class="pginfo">第 ' + donePage + " / " + pages + " 页 · 共 " + doneTotal + " 条</span>";

    $("donePageHint").textContent = "按完成时间倒序 · 每页 " + DONE_PAGE_SIZE + " 条";
  }

  $("donePager").onclick = function (e) {
    var btn = e.target.closest ? e.target.closest("[data-pg]") : null;

    if (!btn || btn.disabled) return;

    var target = donePage + Number(btn.getAttribute("data-pg"));

    if (target < 1 || target > donePageCount() || target === donePage) return;

    donePage = target;

    // 立刻拉一次，不等 3 秒后的那一轮自动刷新 —— 点了没反应会被当成按钮坏了
    refresh();
  };

  $("doneClearBtn").onclick = function () {
    if (doneClearBusy) return;

    if (!confirm("清空「已完成」列表里的全部记录？\n\n只删除记录条目，已经下载的文件不会被删除。")) return;

    var btn = this;
    doneClearBusy = true;
    btn.disabled = true;

    post("api/panel/done/clear", {}).then(function (d) {
      toast("已清理 " + (Number(d.removed) || 0) + " 条记录");

      renderTasks($("done"), [], true);
      $("doneCount").textContent = "共 0 个";
      $("badgeDone").textContent = "0";
      $("badgeDone").className = "nbadge zero";
      $("tileDone").textContent = "0";

      // 清空后当前页必然越界（比如停在第 5 页），回到第一页再回读
      donePage = 1;
      doneTotal = 0;
      renderDonePager();

      // 回读一次跟真库对齐（写线程落地通常几十毫秒就完了）
      setTimeout(refresh, 600);
    }).catch(function (e) {
      if (e.message !== "unauthorized") toast(e.message);
    }).then(function () {
      doneClearBusy = false;
      btn.disabled = false;
    });
  };

  function refresh() {
    return Promise.all([
      api("list_tasks", { state: "downloading", limit: 100 }),
      api("list_tasks", {
        state: "completed",
        limit: DONE_PAGE_SIZE,
        offset: (donePage - 1) * DONE_PAGE_SIZE
      })
    ]).then(function (res) {
      var active = res[0].tasks || [];
      var done = res[1].tasks || [];
      var total = res[1].total;

      // 队列只在「下载中」页渲染一份（概览页那张常驻卡片已撤，改两颗跳转按钮）。
      // 🔴 别再把概览那份加回来：$("active") 已不存在，renderTasks 会拿 null.innerHTML 炸，
      // 而且每 3 秒炸一次、控制台刷屏却只表现为"队列不更新"
      renderTasks($("activeDetail"), active, false);
      renderTasks($("done"), done, true);

      // 页码越界兜底：记录被清理、或在最后一页时库里的条数变少，
      // 都会让当前页落到范围外。这里回退到最后一页并立刻重取
      doneTotal = total == null ? done.length : total;

      if (donePage > donePageCount()) {
        donePage = donePageCount();
        renderDonePager();
        refresh();

        return;
      }

      renderDonePager();

      // 实时速度：只累加「下载中」状态的任务
      var speed = 0;
      var running = 0;
      active.forEach(function (t) {
        var raw = String(t.status || "").toLowerCase();
        if (raw === "downloading") {
          running++;
          speed += Number(t.speed) || 0;
        }
      });

      $("badgeActive").textContent = active.length;
      $("badgeActive").className = "nbadge" + (active.length ? "" : " zero");
      var doneBadge = doneTotal;
      $("badgeDone").textContent = doneBadge;
      $("badgeDone").className = "nbadge" + (doneBadge ? "" : " zero");
      $("tileActive").textContent = active.length;
      $("tileDone").textContent = doneTotal;
      $("dlCount").textContent = active.length + " 个";
      $("doneCount").textContent = "共 " + doneTotal + " 个";

      // 没有记录可清时禁用；正在清理时不覆盖它的禁用状态
      $("doneClearBtn").disabled = doneClearBusy || !doneTotal;
      $("dlHintDetail").textContent = active.length ? "每 3 秒自动刷新" : "";

      // 一键暂停 / 一键取消 / 一键开始：队列空时禁用。busy 期间由 bulkControl 自己
      // 禁着，这里必须把 busy 也算进去 —— 每 3 秒一轮的自动刷新会顺手把按钮重新点亮
      $("dlPauseAll").disabled = bulkBusy || !active.length;
      $("dlCancelAll").disabled = bulkBusy || !active.length;
      $("dlStartAll").disabled = bulkBusy || !active.length;

      // 指标卡：实时速度
      $("metSpeed").innerHTML = (speed / 1024 / 1024).toFixed(1) + '<small class="unit"> MB/s</small>';
      $("metSpeedSub").textContent = running ? running + " 任务合计" : "无活动任务";
      $("metActiveSub").textContent = running ? running + " 个正在下载" : "暂无下载";
    }).catch(function (e) {
      if (e.message !== "unauthorized") {
        $("activeDetail").innerHTML = '<div class="err">' + esc(e.message) + "</div>";
      }
    });
  }

  function refreshLogin() {
    return api("get_login_status", {}).then(function (d) {
      var on = !!d.logged_in;
      var name = d.username || "";

      $("loginDot").className = "dot " + (on ? "on" : "off");
      $("loginText").textContent = on ? ("已登录" + (name ? "：" + name : "")) : "未登录";
      $("biliOutBtn").classList.toggle("hidden", !on);
    }).catch(function () {
      $("loginDot").className = "dot off";
      $("loginText").textContent = "状态未知";
    });
  }

  // ---- 设置 ----
  //
  // 控件完全按服务端下发的 fields 渲染，前端不写死任何一项 ——
  // 以后要加设置项，只改 server.py 的 SETTINGS_FIELDS 即可

  // 优先级列表：默认收起只显示摘要，点开拖动（或箭头）调序；
  // 最终顺序写进同级的隐藏框，collectSettings 照旧取 el.value，不必为它
  // 单独开一条收集路径
  function prioSummaryText(names) {
    return names.slice(0, 3).join(" > ") + (names.length > 3 ? " > …" : "");
  }

  function prioSync(wrap) {
    var rows = Array.prototype.slice.call(wrap.querySelectorAll(".priow"));

    rows.forEach(function (row, i) {
      row.querySelector(".prio-n").textContent = i + 1;
      row.querySelector("[data-mv='-1']").disabled = i === 0;
      row.querySelector("[data-mv='1']").disabled = i === rows.length - 1;
    });

    var input = wrap.querySelector("input[data-key]");

    if (input) {
      // 优先级里是数字档位 id，CDN 列表里是 host 字符串 —— 按 wrap 类型取
      input.value = JSON.stringify(rows.map(function (r) {
        return wrap.classList.contains("cdn")
          ? r.getAttribute("data-host")
          : Number(r.getAttribute("data-id"));
      }));
    }

    // 摘要跟着重排刷新，收起状态看到的始终是当前顺序
    var sumt = wrap.querySelector(".sumt");

    if (sumt) {
      sumt.textContent = prioSummaryText(rows.map(function (r) {
        return r.querySelector(".prio-t").textContent;
      }));
    }
  }

  // options 是 [[host, provider], ...]，拼出「服务商 · 主机」的展示名
  function cdnRowName(options, host) {
    var pair = (options || []).filter(function (p) { return p[0] === host; })[0];

    return (pair && pair[1] ? pair[1] + " · " : "") + host;
  }

  function prioRow(id, name, i, n, isCdn) {
    return '<div class="priow" draggable="true"'
      + (isCdn
        ? ' data-host="' + esc(id) + '"'
        : ' data-id="' + esc(id) + '"') + '>'
      + '<span class="grip" title="拖动调整顺序">&#8942;&#8942;</span>'
      + '<span class="prio-n">' + (i + 1) + "</span>"
      + '<span class="prio-t" title="' + esc(name) + '">' + esc(name) + "</span>"
      + '<button type="button" class="pi" data-mv="-1"'
      + (i === 0 ? " disabled" : "") + ">&uarr;</button>"
      + '<button type="button" class="pi" data-mv="1"'
      + (i === n - 1 ? " disabled" : "") + ">&darr;</button>"
      + "</div>";
  }

  function renderSettings(d) {
    var box = $("setFields");
    var lastGroup = null;

    if (!d.fields || !d.fields.length) {
      box.innerHTML = '<div class="empty">没有可改的设置项</div>';
    } else {
      box.innerHTML = d.fields.map(function (f) {
        var ctl, head = "";

        // MCP 组整体搬去了「MCP 服务器」页（renderMcp 画），设置页不重复出
        if (f.group === "MCP 服务器") return "";

        // 分组标题只在换组时输出一行；组头可点击折叠，行挂 data-g 跟组头联动
        if (f.group && f.group !== lastGroup) {
          head = '<div class="setgroup fold" data-g="' + esc(f.group) + '" role="button"'
            + ' title="点击折叠/展开">'
            + "<span>" + esc(f.group) + '</span><i class="fchev">&#9662;</i></div>';
          lastGroup = f.group;
        }

        if (f.type === "bool") {
          ctl = '<input type="checkbox" data-key="' + esc(f.key) + '"'
            + (f.value ? " checked" : "") + ">";
        } else if (f.type === "enum") {
          // 提交的是下标，value 与 options 同序
          ctl = '<select data-key="' + esc(f.key) + '">'
            + (f.options || []).map(function (text, i) {
              return '<option value="' + i + '"'
                + (f.value === i ? " selected" : "") + ">" + esc(text) + "</option>";
            }).join("")
            + "</select>";
        } else if (f.type === "str") {
          // 密文字段服务端不下发真值，只给 f.set 那一位 —— 输入框留空，用 placeholder
          // 说明"现在有没有值"，留空提交表示不修改（与通知页凭据同一套约定）
          var strValue = f.secret
            ? ' value="" placeholder="' + (f.set ? "已设置 · 留空则不修改" : "未填写") + '"'
            : ' value="' + esc(f.value == null ? "" : f.value) + '"';

          ctl = '<input type="' + (f.secret ? "password" : "text") + '" data-key="' + esc(f.key) + '"'
            + strValue
            + ' autocomplete="off" spellcheck="false" style="width:200px">';
        } else if (f.type === "cdnlist") {
          // 服务商 CDN 列表：结构同优先级（复用拖拽与箭头），但常开不收起，
          // 序号里存的是 host 字符串
          var rows = (f.value || []).map(function (host, i) {
            return prioRow(host, cdnRowName(f.options, host), i, f.value.length, true);
          }).join("");

          ctl = '<div class="prio open cdn"><div class="priolist">' + rows + "</div>"
            + '<input type="hidden" data-key="' + esc(f.key) + '"'
            + ' value="' + esc(JSON.stringify(f.value || [])) + '"></div>';
        } else if (f.type === "priority") {
          var names = {};

          (f.options || []).forEach(function (pair) { names[pair[0]] = pair[1]; });

          var ids = f.value || [];
          var rows = ids.map(function (id, i) {
            return prioRow(id, names[id] || id, i, ids.length);
          }).join("");
          var summary = prioSummaryText(ids.map(function (id) {
            return names[id] || id;
          }));

          ctl = '<div class="prio">'
            + '<button type="button" class="priosum" aria-expanded="false"'
            + ' title="点击展开，拖动或用箭头调整顺序">'
            + '<span class="sumt">' + esc(summary) + "</span>"
            + '<span class="chev">&#9662;</span></button>'
            + '<div class="priolist">' + rows + "</div>"
            + '<input type="hidden" data-key="' + esc(f.key) + '"'
            + ' value="' + esc(JSON.stringify(ids)) + '">'
            + "</div>";
        } else {
          ctl = '<input type="number" data-key="' + esc(f.key) + '"'
            + ' value="' + esc(f.value) + '"'
            + ' min="' + esc(f.min) + '" max="' + esc(f.max) + '"'
            + ' step="' + (f.type === "float" ? "0.1" : "1") + '">'
            + (f.unit ? '<span class="unit">' + esc(f.unit) + "</span>" : "");
        }

        // CDN 列表与优先级一样是高控件列，标签顶对齐
        var tall = f.type === "priority" || f.type === "cdnlist";

        return head + '<div class="setrow' + (tall ? " prio-row" : "") + '"'
          + (f.group ? ' data-g="' + esc(f.group) + '"' : "")
          + '"><div>'
          + '<div class="setname">' + esc(f.label) + "</div>"
          + (f.hint ? '<div class="sethint">' + esc(f.hint) + "</div>" : "")
          + '</div><div class="setctl">' + ctl + "</div></div>";
      }).join("");
    }

    $("setReadonly").innerHTML = (d.readonly || []).map(function (r) {
      return '<div class="setrow"><div class="setname">' + esc(r.label) + "</div>"
        + '<div class="roval">' + esc(r.value) + "</div></div>";
    }).join("");

    // 指标卡里那张「下载目录」复用同一份数据，省一次请求。
    // 按 label 取而不是按下标 —— 以后 readonly 里加项、调序都不会串位
    var ro = {};

    (d.readonly || []).forEach(function (r) { ro[r.label] = r.value; });

    if (ro["下载目录"]) {
      $("ovPath").textContent = ro["下载目录"];
      $("ovPath").title = ro["下载目录"];
    }

    $("ovFree").textContent = ro["剩余空间"] ? "剩余 " + ro["剩余空间"] : "剩余空间 —";
    $("ovFree").title = "下载目录所在分区的可用空间";

    applyFold();
    syncProxyDim();

    renderMcp(d.mcp);
  }

  // ---- 分组折叠：组头点击切换整组显隐，状态记在 localStorage，重渲染后保持 ----
  function foldStore() {
    try { return JSON.parse(localStorage.getItem("setFold") || "{}"); } catch (e) { return {}; }
  }

  function setFold(h, off) {
    var g = h.getAttribute("data-g");

    h.classList.toggle("folded", off);

    document.querySelectorAll('#setFields .setrow[data-g="' + g + '"]').forEach(function (r) {
      r.classList.toggle("gfold", off);
    });
  }

  function applyFold() {
    var saved = foldStore();

    document.querySelectorAll("#setFields .setgroup.fold").forEach(function (h) {
      // 默认全部折叠：只有用户明确展开过（记录为 false）的组才摊开
      setFold(h, saved[h.getAttribute("data-g")] !== false);
    });
  }

  function toggleFold(h) {
    var g = h.getAttribute("data-g");
    var off = !h.classList.contains("folded");

    setFold(h, off);

    var saved = foldStore();

    saved[g] = off;

    try { localStorage.setItem("setFold", JSON.stringify(saved)); } catch (e) {}
  }

  // ---- 代理灰显：非「手动设置代理」模式时，服务器/端口/账号/密码用不上 ----
  var PROXY_DETAIL_KEYS = ["proxy_server", "proxy_port", "proxy_uname", "proxy_password"];

  function syncProxyDim() {
    var sel = document.querySelector('#setFields select[data-key="proxy_mode"]');
    var manual = !!sel && sel.value === "2";  // options 里「手动设置代理」的下标

    PROXY_DETAIL_KEYS.forEach(function (k) {
      var el = document.querySelector('#setFields [data-key="' + k + '"]');

      if (!el) return;

      el.disabled = !manual;
      el.closest(".setrow").classList.toggle("dim", !manual);
    });
  }

  // ---- MCP 服务器组：状态是运行期事实、令牌与客户端配置是敏感只读块，
  // ---- 都不进通用字段渲染，由服务端随 settings 一起下发、这里单独画
  var mcpCache = null;
  var mcpTokenShown = false;

  function mcpClientConfig(m) {
    // 地址取当前访问面板的主机名：面板和 MCP 在同一个容器里，主机名一致
    return JSON.stringify({
      mcpServers: {
        bili23: {
          url: "http://" + (location.hostname || "127.0.0.1") + ":" + m.port + "/mcp",
          headers: { Authorization: "Bearer " + m.token }
        }
      }
    }, null, 2);
  }

  function copyText(t) {
    // 面板走 http:// 局域网地址时没有 navigator.clipboard（仅安全上下文提供），
    // 得退回 execCommand；两条路都试，哪条通走哪条
    var fail = function () {
      var ta = document.createElement("textarea");

      ta.value = t;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();

      try { document.execCommand("copy"); toast("已复制"); }
      catch (e) { toast("复制失败，请手动选择"); }

      document.body.removeChild(ta);
    };

    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(t).then(function () { toast("已复制"); }, fail);
    } else fail();
  }

  function renderMcp(m) {
    var box = $("mcpBox");

    if (!m) { box.innerHTML = '<div class="empty">加载失败</div>'; return; }

    mcpCache = m;
    mcpTokenShown = false;

    var dot = m.running ? "on" : (m.error ? "err" : "off");
    var status = m.running ? "运行中" : (m.error ? "启动失败：" + m.error : "已关闭");

    // 开关与端口带 data-key，collectSettings 一并收走，走 settings/save 落盘；
    // 状态/令牌/客户端配置是运行期事实与敏感只读块，只展示不提交。
    // 令牌默认只给掩码，复制与客户端配置仍用真值 —— 防的是路过的人
    // 和截屏，不是防已登录的持有者
    box.innerHTML = '<div class="setrow"><div><div class="setname">启用 MCP 服务器</div>'
      + '<div class="sethint">让 AI 客户端通过 Model Context Protocol 解析链接并管理下载任务</div></div>'
      + '<div class="setctl"><input type="checkbox" data-key="mcp_enabled"'
      + (m.enabled ? " checked" : "") + "></div></div>"
      + '<div class="setrow"><div><div class="setname">端口</div>'
      + '<div class="sethint">保存后自动重启 MCP 服务器生效</div></div>'
      + '<div class="setctl"><input type="number" data-key="mcp_port"'
      + ' value="' + esc(m.port) + '" min="1024" max="65535"></div></div>'
      + '<div class="setrow"><div><div class="setname">状态</div>'
      + '<div class="sethint">AI 客户端经 ' + esc(String(m.port)) + ' 端口以 JSON-RPC 接入</div></div>'
      + '<div class="setctl"><span class="mcp-status">'
      + '<span class="mcp-dot ' + dot + '"></span>' + esc(status) + "</span></div></div>"
      + '<div class="setrow"><div><div class="setname">访问令牌</div>'
      + '<div class="sethint">每次请求都需要提供，请像密码一样妥善保管；重新生成后旧令牌立即作废</div></div>'
      + '<div class="setctl"><span class="mcp-code" id="mcpTokenText">' + esc(maskToken(m.token)) + "</span>"
      + '<button type="button" class="sm" data-mcp="toggle-token">显示</button>'
      + '<button type="button" class="sm" data-mcp="copy-token">复制</button>'
      + '<button type="button" class="sm" data-mcp="regen-token">重新生成</button></div></div>'
      + '<div class="setrow mcp-widen"><div><div class="setname">客户端配置</div>'
      + '<div class="sethint">复制到 AI 客户端的 MCP 配置里即可使用（地址取当前访问面板的主机名）</div></div>'
      + '<div class="setctl"><pre class="mcp-pre">' + esc(mcpClientConfig(m)) + "</pre>"
      + '<button type="button" class="sm" data-mcp="copy-client">复制</button></div></div>';
  }

  // 令牌掩码：首尾各留 4 位便于核对是不是同一枚，中间全遮。
  // 短令牌直接全遮 —— 留 4 位等于交出去一半
  function maskToken(t) {
    t = String(t || "");

    if (!t) return "（空）";

    return t.length <= 12 ? "••••••••" : t.slice(0, 4) + "••••••••" + t.slice(-4);
  }

  // 渲染每次整体重建，按钮事件照旧走容器委托
  $("mcpBox").addEventListener("click", function (e) {
    var btn = e.target.closest("[data-mcp]");

    if (!btn || !mcpCache) return;

    var act = btn.getAttribute("data-mcp");

    if (act === "toggle-token") {
      mcpTokenShown = !mcpTokenShown;

      $("mcpTokenText").textContent = mcpTokenShown
        ? (mcpCache.token || "（空）") : maskToken(mcpCache.token);

      btn.textContent = mcpTokenShown ? "隐藏" : "显示";
    } else if (act === "copy-token") {
      copyText(mcpCache.token || "");
    } else if (act === "copy-client") {
      copyText(mcpClientConfig(mcpCache));
    } else if (act === "regen-token") {
      btn.disabled = true;

      post("api/panel/mcp_token", {}).then(function (d) {
        renderMcp(d.mcp);
        toast("已重新生成，旧令牌已作废");
      }).catch(function (err) {
        btn.disabled = false;
        toast(err.message || "重新生成失败");
      });
    }
  });

  // 上移/下移与展开收起都用事件委托：列表每次渲染都会重建，绑在容器上
  // 才不会被冲掉。#setFields 这个元素本身不会替换，只换 innerHTML
  $("setFields").addEventListener("click", function (e) {
    // 组头折叠/展开：整组行跟着显隐，状态进 localStorage
    var grp = e.target.closest(".setgroup.fold");

    if (grp) {
      toggleFold(grp);
      return;
    }

    var sum = e.target.closest(".priosum");

    if (sum) {
      var box = sum.closest(".prio");

      box.classList.toggle("open");
      sum.setAttribute("aria-expanded", box.classList.contains("open") ? "true" : "false");
      return;
    }

    var btn = e.target.closest("[data-mv]");

    if (!btn || btn.disabled) return;

    var wrap = btn.closest(".prio");
    var rows = Array.prototype.slice.call(wrap.querySelectorAll(".priow"));
    var from = rows.indexOf(btn.closest(".priow"));
    var to = from + Number(btn.getAttribute("data-mv"));

    if (from < 0 || to < 0 || to >= rows.length || to === from) return;

    // 行的父级是 .priolist 而不是 .prio（外头还包着一层摘要按钮），
    // insertBefore 的参照节点必须取自同一个父级，否则直接抛 NotFoundError
    var list = rows[0].parentNode;

    // 往上移插到目标之前，往下移要插到目标之后，否则等于原地不动
    if (to < from) {
      list.insertBefore(rows[from], rows[to]);
    } else {
      list.insertBefore(rows[from], rows[to + 1]);
    }

    prioSync(wrap);
  });

  // 拖拽排序：HTML5 DnD 没有冒泡问题，但列表会被重建，同样委托到容器上。
  // 触屏浏览器不支持 DnD，箭头按钮是保底手段
  var dragRow = null;

  $("setFields").addEventListener("change", function (e) {
    if (e.target.getAttribute && e.target.getAttribute("data-key") === "proxy_mode") {
      syncProxyDim();
    }
  });

  $("setFields").addEventListener("dragstart", function (e) {
    var row = e.target.closest ? e.target.closest(".priow") : null;

    if (!row) return;

    dragRow = row;
    row.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";

    try {
      e.dataTransfer.setData("text/plain", row.getAttribute("data-id"));
    } catch (err) {}
  });

  $("setFields").addEventListener("dragover", function (e) {
    if (!dragRow) return;

    e.preventDefault();
    e.dataTransfer.dropEffect = "move";

    var over = e.target.closest ? e.target.closest(".priow") : null;

    if (!over || over === dragRow) return;

    // 按指针落在目标行的上半/下半决定插到它前面还是后面
    var rect = over.getBoundingClientRect();
    var before = e.clientY - rect.top < rect.height / 2;

    over.parentNode.insertBefore(dragRow, before ? over : over.nextSibling);
  });

  $("setFields").addEventListener("drop", function (e) {
    if (dragRow) e.preventDefault();
  });

  $("setFields").addEventListener("dragend", function () {
    if (!dragRow) return;

    dragRow.classList.remove("dragging");
    var wrap = dragRow.closest(".prio");

    dragRow = null;

    if (wrap) prioSync(wrap);
  });

  function loadSettings() {
    $("setErr").textContent = "";

    return get("api/panel/settings").then(renderSettings).catch(function (e) {
      if (e.message !== "unauthorized") $("setErr").textContent = e.message;
    });
  }

  function collectSettings() {
    var out = {};

    // MCP 页的开关/端口也带 data-key：两页共用一次保存请求，
    // 改了哪边都整份提交，服务端按 SETTINGS_FIELDS 校验，多余的它不认识
    document.querySelectorAll("#setFields [data-key], #mcpBox [data-key]").forEach(function (el) {
      // 数字框原样交字符串，由服务端统一转型与校验；这里再转一遍只会
      // 把「空输入」悄悄变成 0，用户看到的报错就变成了"不能小于 1"
      out[el.getAttribute("data-key")] = el.type === "checkbox" ? el.checked : el.value;
    });

    return out;
  }

  $("setReloadBtn").onclick = function () {
    loadSettings().then(function () { toast("已重新载入"); });
  };

  $("mcpReloadBtn").onclick = function () {
    loadSettings().then(function () { toast("已重新载入"); });
  };

  // 两页共用一次保存：提交的是整份设置（collectSettings 两页一起收），
  // 服务端按 SETTINGS_FIELDS 校验并落盘，返回值直接喂给重渲染
  function saveSettings(btn, errEl) {
    btn.disabled = true;
    errEl.textContent = "";

    post("api/panel/settings/save", { settings: collectSettings() }).then(function (d) {
      renderSettings(d);
      toast("设置已保存");
    }).catch(function (e) {
      if (e.message !== "unauthorized") errEl.textContent = e.message;
    }).then(function () {
      btn.disabled = false;
    });
  }

  $("setSaveBtn").onclick = function () { saveSettings(this, $("setErr")); };

  $("mcpSaveBtn").onclick = function () { saveSettings(this, $("mcpErr")); };

  // ---- 配置文件设置：导出 / 导入 / 恢复默认 ----

  $("cfgExportBtn").onclick = function () {
    get("api/panel/config_export").then(function (d) {
      var blob = new Blob([JSON.stringify(d.data, null, 4)], { type: "application/json" });
      var a = document.createElement("a");
      var url = URL.createObjectURL(blob);

      a.href = url;
      a.download = "bili23_config.json";
      a.click();
      URL.revokeObjectURL(url);

      // 令牌/Cookie/密码这些敏感项在服务端已替换成掩码，这里说清楚
      // 导出件里为什么是星号，免得用户以为备份坏了
      toast(d.redacted && d.redacted.length
        ? "配置已导出（" + d.redacted.length + " 项敏感配置已隐藏，不含令牌与 Cookie）"
        : "配置已导出");
    }).catch(function (e) {
      if (e.message !== "unauthorized") toast(e.message);
    });
  };

  $("cfgImportBtn").onclick = function () { $("cfgFile").click(); };

  $("cfgFile").addEventListener("change", function () {
    var file = this.files[0];

    this.value = "";

    if (!file) return;

    var reader = new FileReader();

    reader.onload = function () {
      var data;

      try { data = JSON.parse(reader.result); } catch (e) {
        toast("配置文件不是合法的 JSON");

        return;
      }

      post("api/panel/config_import", { data: data }).then(function (d) {
        var msg = "已写入 " + d.applied + " 项";

        if (d.ignored && d.ignored.length) msg += "，跳过 " + d.ignored.length + " 项";

        toast(msg);
        loadSettings();
      }).catch(function (e) {
        if (e.message !== "unauthorized") toast(e.message);
      });
    };

    reader.readAsText(file);
  });

  $("cfgResetBtn").onclick = function () {
    if (!confirm("确定把全部设置恢复为默认值吗？\n\n"
      + "· 当面板账号将回到默认 admin / password，需要重新登录；\n"
      + "· 面板访问令牌与 MCP 令牌都会换新，旧令牌立即作废；\n"
      + "· 当前配置被覆盖，立即生效且无法撤销。")) return;

    this.disabled = true;

    post("api/panel/config_reset", {}).then(function () {
      toast("已恢复默认配置，请用默认账号重新登录");
      loadSettings();
    }).catch(function (e) {
      if (e.message !== "unauthorized") toast(e.message);
    }).then(function () {
      this.disabled = false;
    }.bind(this));
  };

  // ---- 命名规则 ----
  //
  // 服务端是唯一的事实来源：规则串的校验与预览都在那边做（语法 + 变量存在性 +
  // 可选段 + 真渲染一次），页面不重写一份 —— 网页放行、运行期却渲染不出来的
  // 规则，用户在界面上看不到任何提示，任务建得出来而文件名是空的。
  //
  // 编辑区与列表是**实时绑定**的：输入即写进 nmState.rules[sel]，切行不需要
  // 先"提交"，也就不会出现"切走了才发现没保存"的错位。

  var nmState = {
    types: [], rules: [], origin: [],
    builtin: {}, builtinList: [], builtinByType: {},
    sel: -1, loaded: false, timer: null, seq: 0,
  };

  function nmCur() { return nmState.sel >= 0 ? nmState.rules[nmState.sel] : null; }

  function nmTypeInfo(typeId) {
    for (var i = 0; i < nmState.types.length; i++) {
      if (nmState.types[i].value === typeId) return nmState.types[i];
    }

    return null;
  }

  function nmTypeLabel(typeId) {
    var info = nmTypeInfo(typeId);

    return info ? info.label : String(typeId);
  }

  // 内置规则按 id 匹配 —— id 是配置里写死的固定 UUID，自建规则是当场生成的，
  // 因此不必额外加字段就能分清"这条有没有出厂值可恢复"
  function nmBuiltinOf(rule) {
    return rule && rule.id ? (nmState.builtin[rule.id] || null) : null;
  }

  function nmDirty() {
    return JSON.stringify(nmState.rules) !== JSON.stringify(nmState.origin);
  }

  function nmMark() {
    $("nmNote").textContent = nmDirty() ? "● 有未保存的改动" : "";
  }

  function nmRenderList() {
    var kw = ($("nmSearch").value || "").trim().toLowerCase();
    var box = $("nmItems");

    $("nmCount").textContent = nmState.rules.length ? nmState.rules.length + " 条规则" : "";

    if (!nmState.rules.length) {
      box.innerHTML = '<div class="empty">还没有任何规则</div>';
      return;
    }

    var html = "";

    nmState.rules.forEach(function (r, i) {
      // 匹配名称、类型与规则串三项：规则串不在列表里，但用户找规则时记得住的
      // 往往正是它。筛掉的行不进 HTML，但 data-i 仍用真实下标 —— 选中、删除
      // 全按全量下标走，筛过的行号与数组错位是这类列表最常见的 bug
      var hay = (r.name + " " + nmTypeLabel(r.type) + " " + r.rule).toLowerCase();

      if (kw && hay.indexOf(kw) < 0) return;

      html += '<button type="button" class="nmi' + (i === nmState.sel ? " on" : "")
        + '" data-i="' + i + '" title="' + esc(r.name + "\n" + r.rule) + '">'
        + '<span class="t">' + esc(r.name || "（未命名）") + "</span>"
        + (r.default ? '<span class="df">✓</span>' : "")
        + '<span class="ty">' + esc(nmTypeLabel(r.type)) + "</span></button>";
    });

    box.innerHTML = html || '<div class="empty">没有匹配的规则</div>';

    // 新建 / 复制出来的那条总落在列表末尾，可能还在滚动区外。只滚列表本身、
    // 不动整个页面 —— 不把它带进视野，用户会以为按钮没生效
    var current = box.querySelector(".nmi.on");

    if (current) {
      var boxRect = box.getBoundingClientRect();
      var itemRect = current.getBoundingClientRect();

      if (itemRect.top < boxRect.top) box.scrollTop -= boxRect.top - itemRect.top;
      else if (itemRect.bottom > boxRect.bottom) box.scrollTop += itemRect.bottom - boxRect.bottom;
    }
  }

  function nmRenderEditor() {
    var r = nmCur();

    $("nmEdit").style.display = r ? "" : "none";

    if (!r) return;

    $("nmName").value = r.name;
    $("nmType").value = String(r.type);
    $("nmDef").checked = !!r.default;

    // 默认规则是这一类型的兜底：改类型会让原类型失去默认规则，取消勾选同理。
    // 与桌面端一样把这两个动作锁死，而不是等人改完再报错
    $("nmType").disabled = !!r.default;
    $("nmDef").disabled = !!r.default;
    $("nmRule").value = r.rule;
    $("nmRestoreBtn").disabled = !nmBuiltinOf(r);

    nmRenderVars();
    nmRenderList();
    nmPreview();
    nmMark();
  }

  function nmRenderVars() {
    var r = nmCur();
    var info = r ? nmTypeInfo(r.type) : null;

    if (!info) { $("nmVars").innerHTML = ""; $("nmVarHint").textContent = ""; return; }

    $("nmVarHint").textContent = "（" + info.label + " 常用的 " + info.vars.length + " 个，点击插入）";

    $("nmVars").innerHTML = info.vars.map(function (v) {
      // {year} / {tmdb_id} 是空串抽样（它们本来就得靠「名称识别」才有值），
      // 空的时候别把"例："孤零零地留在提示里
      var tip = v.example ? v.desc + "　例：" + v.example : v.desc;

      return '<button type="button" class="nvc" data-v="' + esc(v.var) + '" title="'
        + esc(tip) + '">' + esc(v.var) + "<i>" + esc(v.desc) + "</i></button>";
    }).join("");
  }

  function nmInsertVar(text) {
    var input = $("nmRule");
    var start = input.selectionStart == null ? input.value.length : input.selectionStart;
    var end = input.selectionEnd == null ? start : input.selectionEnd;

    input.value = input.value.slice(0, start) + text + input.value.slice(end);

    var caret = start + text.length;
    input.focus();
    input.setSelectionRange(caret, caret);

    nmCur().rule = input.value;
    nmPreview();
    nmMark();
  }

  function nmPaintPreview(d) {
    $("nmErr").textContent = d.valid ? "" : d.error;

    $("nmPreview").innerHTML = d.valid
      ? (d.rows || []).map(function (row) {
          return '<div class="pvr"><span class="pvl">' + esc(row.label) + '</span>'
            + '<span class="pvv' + (row.path ? "" : " bad") + '">'
            + esc(row.path || "（渲染不出文件名）") + "</span></div>";
        }).join("")
      : "";

    var cur = nmCur();
    var info = cur ? nmTypeInfo(cur.type) : null;

    // 来源类列表里混着的影视 / 课程条目会整条改用它们自己的规则。不说一句，
    // 用户会以为整个收藏夹都归手里这条规则管
    $("nmMixed").textContent = (info && info.mixed.length)
      ? "注：" + info.mixed.join(" / ") + " 类型的条目按它们自己的规则命名，不走这一条。"
      : "";
  }

  function nmPreview() {
    var r = nmCur();

    if (nmState.timer) clearTimeout(nmState.timer);

    if (!r) { $("nmErr").textContent = ""; $("nmPreview").innerHTML = ""; return; }

    // 空规则不请求：那必然回一句"不能为空"，用户清空重打的一瞬间不必看见它
    if (!r.rule) {
      $("nmErr").textContent = "";
      $("nmPreview").innerHTML = '<div class="pvr"><span class="pvl">—</span>'
        + '<span class="pvv bad">规则为空</span></div>';
      nmPaintMixedOnly();
      return;
    }

    // 边打边预览，防抖 320ms；序号只认最新那一次，慢响应回来时不覆盖后发的结果
    var mine = ++nmState.seq;

    nmState.timer = setTimeout(function () {
      post("api/panel/naming/preview", { rule: r.rule, type: r.type }).then(function (d) {
        if (mine !== nmState.seq) return;

        nmPaintPreview(d);
      }).catch(function (e) {
        if (mine !== nmState.seq || e.message === "unauthorized") return;

        $("nmErr").textContent = e.message;
      });
    }, 320);
  }

  function nmPaintMixedOnly() {
    var cur = nmCur();
    var info = cur ? nmTypeInfo(cur.type) : null;

    $("nmMixed").textContent = (info && info.mixed.length)
      ? "注：" + info.mixed.join(" / ") + " 类型的条目按它们自己的规则命名，不走这一条。"
      : "";
  }

  $("nmItems").addEventListener("click", function (e) {
    var item = e.target.closest(".nmi");

    if (!item) return;

    var index = Number(item.getAttribute("data-i"));

    if (index === nmState.sel) return;

    nmState.sel = index;
    nmRenderEditor();
  });

  $("nmSearch").addEventListener("input", nmRenderList);

  $("nmName").addEventListener("input", function () {
    var r = nmCur();

    if (!r) return;

    r.name = this.value;
    nmRenderList();
    nmMark();
  });

  $("nmRule").addEventListener("input", function () {
    var r = nmCur();

    if (!r) return;

    r.rule = this.value;
    nmPreview();
    nmMark();
  });

  $("nmType").addEventListener("change", function () {
    var r = nmCur();

    if (!r) return;

    r.type = Number(this.value);
    nmRenderVars();
    nmPreview();
    nmRenderList();
    nmMark();
  });

  $("nmDef").addEventListener("change", function () {
    var r = nmCur();

    if (!r) return;

    r.default = this.checked;

    // 同类型的默认规则是排他的：勾了这条就把同类型的另一条取消，
    // 与桌面端 _set_default_rule 收敛到一条的做法一致
    if (r.default) {
      nmState.rules.forEach(function (other) {
        if (other !== r && other.type === r.type) other.default = false;
      });
    }

    nmRenderList();
    nmMark();
  });

  $("nmVars").addEventListener("click", function (e) {
    var chip = e.target.closest(".nvc");

    if (chip) nmInsertVar(chip.getAttribute("data-v"));
  });

  function nmSeedRule(typeId) {
    // 新规则的初始串取该类型内置默认规则的内容。一律套 {leaf_title} 的话，
    // 剧集 / 课程这些类型根本没有这个变量，新建出来就是一条渲染不出东西的规则
    var entry = nmState.builtinByType[typeId];

    return entry ? entry.rule : "{leaf_title}";
  }

  function nmFocusNew() {
    $("nmSearch").value = "";
    nmRenderEditor();
    $("nmName").focus();
    $("nmName").select();
  }

  $("nmAddBtn").onclick = function () {
    var cur = nmCur();

    // 新规则跟随当前选中项的类型：正在给剧集写规则时点「添加」，
    // 得到的却是一条单视频规则，还得手动改一次
    var typeId = cur ? cur.type : (nmState.types[0] ? nmState.types[0].value : 11);

    nmState.rules.push({
      id: "", name: "新建规则", name_key: "",
      type: typeId, rule: nmSeedRule(typeId), default: false,
    });

    nmState.sel = nmState.rules.length - 1;
    nmFocusNew();
    nmMark();
  };

  $("nmDupBtn").onclick = function () {
    var r = nmCur();

    if (!r) return;

    // id 留空：由服务端补一个新的。网页上拿不到 crypto.randomUUID
    //（它要求安全上下文，局域网 http 下是 undefined），而 id 是"恢复内置默认"
    // 的唯一匹配依据，不能让它随机退化成一个会被当成内置规则的假 id
    nmState.rules.push({
      id: "", name: r.name + "（副本）", name_key: "",
      type: r.type, rule: r.rule, default: false,
    });

    nmState.sel = nmState.rules.length - 1;
    nmFocusNew();
    nmMark();
  };

  $("nmDelBtn").onclick = function () {
    var r = nmCur();

    if (!r) return;

    // 默认规则删不得：删掉之后该类型就没有规则可用了，运行期会退化成
    // {leaf_title}，文件名全错却全程不报错。桌面端同样把这条锁死
    if (r.default) {
      toast("默认规则不能删除；要换掉它，请把同类型的另一条设为默认");
      return;
    }

    if (!confirm("删除规则「" + r.name + "」？保存后生效。")) return;

    nmState.rules.splice(nmState.sel, 1);
    nmState.sel = Math.min(nmState.sel, nmState.rules.length - 1);
    nmRenderEditor();
    nmMark();
  };

  $("nmRestoreBtn").onclick = function () {
    var r = nmCur();
    var builtin = nmBuiltinOf(r);

    if (!builtin) return;

    // 只还原这一条：预设被改坏了却要连自己写的规则一起陪葬，是这类窗口最招骂的
    // 设计。名字也一并还原 —— 内置规则的名字存的是翻译键，还原后仍随界面语言走
    r.rule = builtin.rule;
    r.name = builtin.name;
    r.name_key = builtin.name_key;

    nmRenderEditor();
    nmMark();
    toast("已恢复这条规则的内置默认值");
  };

  $("nmResetAllBtn").onclick = function () {
    if (!confirm("把全部命名规则恢复为内置默认值？\n\n"
      + "· 自建的规则会一并丢弃；\n"
      + "· 点「保存」后才真正生效。")) return;

    nmState.rules = nmState.builtinList.map(function (b) {
      return {
        id: b.id, name: b.name, name_key: b.name_key,
        type: b.type, rule: b.rule, default: true,
      };
    });

    nmState.sel = nmState.rules.length ? 0 : -1;
    $("nmSearch").value = "";
    nmRenderEditor();
    nmMark();
    toast("已载入内置默认规则，点「保存」生效");
  };

  $("nmSaveBtn").onclick = function () {
    var btn = this;

    // 名称被删空这类问题客户端就能看出来，先拦下来省一次往返；
    // 规则串的校验仍在服务端做（那是唯一的事实来源）
    for (var i = 0; i < nmState.rules.length; i++) {
      if (!String(nmState.rules[i].name || "").trim()) {
        toast("第 " + (i + 1) + " 条规则的名称不能为空");
        nmState.sel = i;
        nmRenderEditor();
        return;
      }
    }

    btn.disabled = true;

    post("api/panel/naming/save", { rules: nmState.rules }).then(function (d) {
      // 以服务端回的表为准：它补过新规则的 id、也收敛过默认标记
      nmState.rules = d.rules || [];
      nmState.origin = JSON.parse(JSON.stringify(nmState.rules));
      nmState.sel = nmState.rules.length
        ? Math.min(Math.max(nmState.sel, 0), nmState.rules.length - 1) : -1;

      nmRenderEditor();
      toast("命名规则已保存，对之后新建的任务生效");
    }).catch(function (e) {
      if (e.message !== "unauthorized") toast(e.message);
    }).then(function () {
      btn.disabled = false;
    });
  };

  $("nmReloadBtn").onclick = function () {
    if (nmDirty() && !confirm("有未保存的改动，重新载入会丢弃它们。继续？")) return;

    loadNaming();
  };

  function loadNaming() {
    return get("api/panel/naming").then(function (d) {
      nmState.types = d.types || [];
      nmState.rules = d.rules || [];
      nmState.origin = JSON.parse(JSON.stringify(nmState.rules));

      nmState.builtinList = d.builtin || [];
      nmState.builtin = {};
      nmState.builtinByType = {};

      nmState.builtinList.forEach(function (b) {
        nmState.builtin[b.id] = b;
        nmState.builtinByType[b.type] = b;
      });

      $("nmType").innerHTML = nmState.types.map(function (t) {
        return '<option value="' + t.value + '">' + esc(t.label) + "</option>";
      }).join("");

      nmState.loaded = true;
      nmState.sel = nmState.rules.length
        ? Math.min(Math.max(nmState.sel, 0), nmState.rules.length - 1) : -1;

      nmRenderEditor();
    }).catch(function (e) {
      if (e.message !== "unauthorized") {
        $("nmItems").innerHTML = '<div class="empty">' + esc(e.message) + "</div>";
      }
    });
  }

  // ---- 名称识别 ----
  //
  // 与命名规则页同一套路：服务端是唯一的事实来源。匹配与应用跑的是运行期命名用的
  // 那一份实现（util/common/naming_alias.py），页面不另写一套 —— 网页放行、下载时
  // 却不生效的规则，用户在界面上看不到任何提示。
  //
  // 编辑区与列表实时绑定（输入即写回选中项）；预览走服务端，防抖 320ms ——
  // 它要真渲染一次文件名，本地做不了。

  var alState = {
    fields: [], modes: [], aliases: [], origin: [],
    bangumiRule: "", sel: -1, loaded: false, timer: null, seq: 0,
  };

  function alCur() { return alState.sel >= 0 ? alState.aliases[alState.sel] : null; }

  function alFieldLabel(value) {
    for (var i = 0; i < alState.fields.length; i++) {
      if (alState.fields[i].value === value) return alState.fields[i].label;
    }

    return String(value);
  }

  function alDirty() {
    return JSON.stringify(alState.aliases) !== JSON.stringify(alState.origin);
  }

  function alMark() {
    $("alMsg").textContent = alDirty() ? "● 有未保存的改动" : "";
  }

  // 列表行的一句话摘要：变成什么
  function alSummary(a) {
    var parts = [];

    if (a.title) parts.push(a.title);
    if (a.season !== null && a.season !== undefined && a.season !== "") parts.push("第 " + a.season + " 季");
    if (a.year) parts.push(a.year);
    if (a.tmdb) parts.push("{tmdb-" + a.tmdb + "}");

    return parts.join(" · ");
  }

  function alRenderList() {
    var kw = ($("alSearch").value || "").trim().toLowerCase();
    var box = $("alItems");

    $("alCount").textContent = alState.aliases.length
      ? alState.aliases.length + " 条规则" : "";

    if (!alState.aliases.length) {
      box.innerHTML = '<div class="empty">还没有识别规则</div>';
      return;
    }

    var html = "";

    alState.aliases.forEach(function (a, i) {
      // 筛掉的行不进 HTML，但 data-i 仍用真实下标 —— 选中与删除全按全量下标走，
      // 筛过的行号与数组错位是这类列表最常见的 bug
      var hay = (a.match + " " + a.title + " " + a.note + " " + alSummary(a)).toLowerCase();

      if (kw && hay.indexOf(kw) < 0) return;

      html += '<button type="button" class="nmi'
        + (i === alState.sel ? " on" : "")
        + (a.shadowed ? " bad" : "")
        + '" data-i="' + i + '" title="'
        + esc(a.match + "\n" + (alSummary(a) || "（不改任何东西）")
          + (a.shadowed ? "\n⚠ 被上面的规则遮住了，永远不会生效" : ""))
        + '">'
        + '<span class="t">' + esc(a.match || "（匹配内容为空）") + "</span>"
        + (a.enabled ? "" : '<span class="ty">停用</span>')
        + '<span class="ty">' + esc(alFieldLabel(a.field)) + "</span></button>";
    });

    box.innerHTML = html || '<div class="empty">没有匹配的规则</div>';

    // 新建 / 复制的落在末尾，可能还在滚动区外。只滚列表本身，不动整个页面
    var current = box.querySelector(".nmi.on");

    if (current) {
      var boxRect = box.getBoundingClientRect();
      var itemRect = current.getBoundingClientRect();

      if (itemRect.top < boxRect.top) box.scrollTop -= boxRect.top - itemRect.top;
      else if (itemRect.bottom > boxRect.bottom) box.scrollTop += itemRect.bottom - boxRect.bottom;
    }
  }

  // 与服务端 naming_alias.shadowed_indexes 同一判据：contains 模式下，前面某条的
  // 匹配内容是当前这条的子串 ⇒ 当前这条永远轮不到。上移下移后本地先重算一遍，
  // 不必等服务端回来才知道标错人了
  function alRecomputeShadow() {
    alState.aliases.forEach(function (a, i) {
      a.shadowed = false;

      if (a.mode !== "contains" || !a.match) return;

      for (var j = 0; j < i; j++) {
        var prev = alState.aliases[j];

        if (prev.mode !== "contains" || !prev.match) continue;

        var reachable = prev.field === a.field
          || prev.field === "any" || a.field === "any";

        if (reachable && a.match.indexOf(prev.match) >= 0) {
          a.shadowed = true;
          break;
        }
      }
    });
  }

  function alRenderEditor() {
    var a = alCur();

    $("alEdit").style.display = a ? "" : "none";

    if (!a) return;

    $("alField").value = a.field;
    $("alMode").value = a.mode;
    $("alMatch").value = a.match;
    $("alTitle").value = a.title;
    $("alSeason").value = (a.season === null || a.season === undefined) ? "" : a.season;
    $("alYear").value = a.year;
    $("alTmdb").value = a.tmdb;
    $("alNoteIn").value = a.note;
    $("alEnabled").checked = !!a.enabled;

    // 上一次「已填入 xxx」在换了规则之后就不成立了，收回静态说明 ——
    // 留着的话会出现"表单是西游记、提示却在说巴啦啦小魔仙"
    alTmdbSay("");

    alRenderList();
    alPreview();
    alMark();
  }

  function alPaintPreview(d) {
    $("alErr").textContent = d.valid ? "" : d.error;

    if (!d.valid) {
      $("alPreview").innerHTML = "";
      $("alTip").textContent = "";
      return;
    }

    var keys = [
      ["season_title", "季标题"],
      ["series_title", "系列标题"],
      ["season_number", "季号"],
      ["year", "年份"],
      ["tmdb_id", "TMDB 编号"],
    ];

    function text(value) {
      return (value === null || value === undefined || value === "") ? "—" : String(value);
    }

    function block(title, snap, ref, path) {
      var lines = keys.map(function (k) {
        var value = text(snap[k[0]]);
        var cls = "v" + (value === "—" ? " nil" : "");

        // 与识别前不同的行上色，否则两组值看起来只是重复了一遍
        if (ref && value !== text(ref[k[0]])) cls += " chg";

        return '<div class="idline"><span class="k">' + k[1] + "</span>"
          + '<span class="' + cls + '">' + esc(value) + "</span></div>";
      }).join("");

      var pathLine = '<div class="idline"><span class="k">文件名</span>'
        + '<span class="v pth' + (path ? "" : " bad") + '">'
        + esc(path || "（渲染不出文件名）") + "</span></div>";

      return '<div class="idcmp"><div class="h">' + title + "</div>" + lines + pathLine + "</div>";
    }

    $("alPreview").innerHTML =
      block("识别前", d.before || {}, null, d.path_before)
      + block("识别后", d.after || {}, d.before || {}, d.path_after);

    // 识别只提供变量，落到文件名上还得命名规则里引用它们。不说这一句，
    // 用户会以为加了识别规则文件名就会变
    if (!d.rule) {
      $("alTip").textContent = "注：影视类型当前没有默认命名规则，预览里的文件名渲染不出来。";
    } else {
      var miss = [];

      if (d.rule.indexOf("{year}") < 0) miss.push("{year}");
      if (d.rule.indexOf("{tmdb_id") < 0) miss.push("{tmdb_id}");

      $("alTip").textContent = miss.length
        ? "注：影视的命名规则里还没引用 " + miss.join(" / ")
          + "，识别结果不会出现在文件名中 —— 去「命名规则」页把它们加进模板。"
        : "";
    }
  }

  function alPreview() {
    var a = alCur();

    if (alState.timer) clearTimeout(alState.timer);

    if (!a) {
      $("alErr").textContent = "";
      $("alPreview").innerHTML = "";
      $("alTip").textContent = "";
      return;
    }

    // 空匹配内容不请求：那必然回一句"不能为空"，用户清空重打的一瞬间不必看见它
    if (!String(a.match || "").trim()) {
      $("alErr").textContent = "";
      $("alPreview").innerHTML = "";
      $("alTip").textContent = "填上匹配内容才有预览。";
      return;
    }

    // 边打边预览，序号只认最新那一次，慢响应回来时不覆盖后发的结果
    var mine = ++alState.seq;

    alState.timer = setTimeout(function () {
      post("api/panel/identify/preview", { alias: a }).then(function (d) {
        if (mine !== alState.seq) return;

        alPaintPreview(d);
      }).catch(function (e) {
        if (mine !== alState.seq || e.message === "unauthorized") return;

        $("alErr").textContent = e.message;
      });
    }, 320);
  }

  $("alItems").addEventListener("click", function (e) {
    var item = e.target.closest(".nmi");

    if (!item) return;

    var index = Number(item.getAttribute("data-i"));

    if (index === alState.sel) return;

    alState.sel = index;
    alRenderEditor();
  });

  $("alSearch").addEventListener("input", alRenderList);

  // 几个文本输入框的读值逻辑一样，按字段名批量绑定，免得漏掉一个 ——
  // 漏掉的那个表现是"改了没反应"，最难查
  [
    ["alMatch", "match"],
    ["alTitle", "title"],
    ["alYear", "year"],
    ["alTmdb", "tmdb"],
    ["alNoteIn", "note"],
  ].forEach(function (pair) {
    $(pair[0]).addEventListener("input", function () {
      var a = alCur();

      if (!a) return;

      a[pair[1]] = this.value;
      alRenderList();
      alPreview();
      alMark();
    });
  });

  $("alSeason").addEventListener("input", function () {
    var a = alCur();

    if (!a) return;

    // 合法的数字就地转成 number，与服务端读回来的类型一致 —— 否则每次保存后
    // 本地都因为 "2" !== 2 而显示"有未保存的改动"
    var raw = this.value.trim();

    a.season = raw === "" ? "" : (/^\d+$/.test(raw) ? Number(raw) : raw);

    alRenderList();
    alPreview();
    alMark();
  });

  // TMDB 链接 → 匹配内容 / 剧名 / 年份 / 编号 / 季号，省得对着 TMDB 页面手抄四个框。
  //
  // 匹配内容也一并填上：多数片子 B站那边的季标题与 TMDB 名称一致，「获取」一次
  // 就能存规则；不一致的（西游记续集 / 巴啦啦小魔仙…）拿 TMDB 名字去匹配会一直
  // 不命中，所以提示里要点名「对不上时改一下」，别让人以为填完就万事大吉。
  // 覆盖已有匹配内容属于「获取」的本意，且可逆 —— 点「重新载入」即还原
  //
  // 没话可说时回到这句静态说明（HTML 里初始就写着它）：空着会让这一行塌成空白
  var AL_TMDB_HINT = "匹配内容默认取 TMDB 名称 —— 对不上 B站那边的季标题时改一下";

  function alTmdbSay(text, bad) {
    var box = $("alTmdbMsg");

    box.textContent = text || AL_TMDB_HINT;
    box.className = bad ? "hint bad" : "hint";
  }

  $("alTmdbFill").addEventListener("click", function () {
    var a = alCur();

    if (!a) return;

    var link = $("alTmdbUrl").value.trim();

    if (!link) {
      alTmdbSay("先粘一条 TMDB 链接，例如 https://www.themoviedb.org/tv/62591", true);
      return;
    }

    var button = this;
    var label = button.textContent;

    button.disabled = true;
    button.textContent = "获取中…";
    alTmdbSay("正在取 TMDB…", false);

    function restore() {
      button.disabled = false;
      button.textContent = label;
    }

    post("api/panel/identify/tmdb", { link: link }).then(function (d) {
      if (!d.ok) {
        alTmdbSay(d.error || "没取到信息", true);
        return;
      }

      // 匹配内容一并填 TMDB 名称。原本已有别的值（多半是手调过的）就记下来、
      // 在提示里点一句 —— 覆盖是「获取」的本意，但得让人知道怎么退回去
      var before = (a.match || "").trim();

      a.match = d.title;
      a.title = d.title;
      a.year = d.year;
      a.tmdb = d.tmdb;

      if (d.season) a.season = Number(d.season);

      // 重填整块编辑器：逐个 input 赋值容易漏掉一个，也让列表与预览一次到位
      alRenderEditor();

      alTmdbSay(
        "已填入：" + d.title + (d.year ? "（" + d.year + "）" : "")
          + " · TMDB " + d.tmdb
          + (d.season_from_url
            ? " · 季号取自链接"
            : " · 季号按 1 填（链接里带 /season/N 才是 TMDB 的季）")
          + (before && before !== d.title
            ? " · 匹配内容 " + before + " → " + d.title + "（点「重新载入」可还原）"
            : " · 匹配内容也用的 TMDB 名称"),
        false
      );
    }).catch(function (e) {
      alTmdbSay(e.message === "unauthorized" ? "登录已过期，重新登录后再试" : e.message, true);
    }).then(restore, restore);
  });

  $("alField").addEventListener("change", function () {
    var a = alCur();

    if (!a) return;

    a.field = this.value;
    alRecomputeShadow();
    alRenderList();
    alPreview();
    alMark();
  });

  $("alMode").addEventListener("change", function () {
    var a = alCur();

    if (!a) return;

    a.mode = this.value;
    alRecomputeShadow();
    alRenderList();
    alPreview();
    alMark();
  });

  $("alEnabled").addEventListener("change", function () {
    var a = alCur();

    if (!a) return;

    a.enabled = this.checked;
    alRenderList();
    alPreview();
    alMark();
  });

  function alFocusNew() {
    $("alSearch").value = "";
    alRenderEditor();
    $("alMatch").focus();
    $("alMatch").select();
  }

  $("alAddBtn").onclick = function () {
    var cur = alCur();

    alState.aliases.push({
      id: "", enabled: true,
      // 匹配位置与方式跟随当前选中项：正在给季标题配规则时点「添加」，
      // 得到的却是「系列标题」的规则，还得手动改一次
      field: cur ? cur.field : "season_title",
      mode: cur ? cur.mode : "contains",
      match: "", title: "", season: "", year: "", tmdb: "", note: "",
      shadowed: false,
    });

    alState.sel = alState.aliases.length - 1;
    alFocusNew();
    alMark();
  };

  $("alDupBtn").onclick = function () {
    var a = alCur();

    if (!a) return;

    // id 留空：由服务端补一个新的。网页上拿不到 crypto.randomUUID
    //（它要求安全上下文，局域网 http 下是 undefined）
    alState.aliases.push({
      id: "", enabled: a.enabled,
      field: a.field, mode: a.mode,
      match: a.match + "（副本）",
      title: a.title, season: a.season, year: a.year, tmdb: a.tmdb, note: a.note,
      shadowed: false,
    });

    alState.sel = alState.aliases.length - 1;
    alFocusNew();
    alMark();
  };

  $("alDelBtn").onclick = function () {
    var a = alCur();

    if (!a) return;

    if (!confirm("删除识别规则「" + (a.match || "未命名") + "」？保存后生效。")) return;

    alState.aliases.splice(alState.sel, 1);
    alState.sel = Math.min(alState.sel, alState.aliases.length - 1);
    alRecomputeShadow();
    alRenderEditor();
    alMark();
  };

  function alMove(step) {
    var a = alCur();

    if (!a) return;

    var to = alState.sel + step;

    if (to < 0 || to >= alState.aliases.length) {
      toast(step < 0 ? "已经在最上面了" : "已经在最下面了");
      return;
    }

    alState.aliases.splice(alState.sel, 1);
    alState.aliases.splice(to, 0, a);
    alState.sel = to;

    alRecomputeShadow();
    alRenderEditor();
    alMark();
  }

  $("alUpBtn").onclick = function () { alMove(-1); };
  $("alDownBtn").onclick = function () { alMove(1); };

  $("alSaveBtn").onclick = function () {
    var btn = this;

    // 匹配内容为空是最常见的漏填，客户端先拦下来省一次往返；
    // 其余校验（正则合法性、季号范围、有没有改到东西）仍在服务端做
    for (var i = 0; i < alState.aliases.length; i++) {
      if (!String(alState.aliases[i].match || "").trim()) {
        toast("第 " + (i + 1) + " 条规则的匹配内容不能为空");
        alState.sel = i;
        alRenderEditor();
        return;
      }
    }

    btn.disabled = true;

    post("api/panel/identify/save", { aliases: alState.aliases }).then(function (d) {
      // 以服务端回的表为准：它补过 id，也重算过遮蔽标记
      alState.aliases = d.aliases || [];
      alState.origin = JSON.parse(JSON.stringify(alState.aliases));
      alState.sel = alState.aliases.length
        ? Math.min(Math.max(alState.sel, 0), alState.aliases.length - 1) : -1;

      alRenderEditor();
      toast("名称识别规则已保存，对之后新建的任务生效");
    }).catch(function (e) {
      if (e.message !== "unauthorized") toast(e.message);
    }).then(function () {
      btn.disabled = false;
    });
  };

  $("alReloadBtn").onclick = function () {
    if (alDirty() && !confirm("有未保存的改动，重新载入会丢弃它们。继续？")) return;

    loadIdentify();
  };

  function loadIdentify() {
    return get("api/panel/identify").then(function (d) {
      alState.fields = d.fields || [];
      alState.modes = d.modes || [];
      alState.bangumiRule = d.bangumi_rule || "";
      alState.aliases = d.aliases || [];
      alState.origin = JSON.parse(JSON.stringify(alState.aliases));

      $("alField").innerHTML = alState.fields.map(function (f) {
        return '<option value="' + f.value + '">' + esc(f.label) + "</option>";
      }).join("");

      $("alMode").innerHTML = alState.modes.map(function (m) {
        return '<option value="' + m.value + '">' + esc(m.label) + "</option>";
      }).join("");

      alState.loaded = true;
      alState.sel = alState.aliases.length
        ? Math.min(Math.max(alState.sel, 0), alState.aliases.length - 1) : -1;

      alRenderEditor();
    }).catch(function (e) {
      if (e.message !== "unauthorized") {
        $("alItems").innerHTML = '<div class="empty">' + esc(e.message) + "</div>";
      }
    });
  }

  // ---- 日志 ----

  function levelClass(line) {
    var m = /\] - [^-]+ - ([A-Z]+) - at /.exec(line);

    if (!m) return "";

    if (m[1] === "ERROR" || m[1] === "CRITICAL") return "lv-err";
    if (m[1] === "WARNING") return "lv-warn";
    if (m[1] === "DEBUG") return "lv-dim";

    return "";
  }

  function renderLogFiles(d) {
    var sel = $("logFile");
    var names = (d.files || []).map(function (f) { return f.key; });
    var have = Array.prototype.map.call(sel.options, function (o) { return o.value; });

    // 选项没变就不重建：否则每次自动刷新都会把用户刚选的文件顶掉
    if (names.join("|") !== have.join("|")) {
      sel.innerHTML = names.map(function (n) {
        return '<option value="' + esc(n) + '">' + esc(n) + "</option>";
      }).join("");
    }

    if (d.file) sel.value = d.file;
  }

  function renderLogBody(d) {
    var box = $("logBox");
    var lines = d.lines || [];
    var stick = $("logAuto").checked || box.scrollTop + box.clientHeight >= box.scrollHeight - 24;

    if (!lines.length) {
      box.innerHTML = '<span class="lv-dim">（没有匹配的日志行）</span>';
    } else {
      box.innerHTML = lines.map(function (l) {
        var c = levelClass(l);

        return c ? '<span class="' + c + '">' + esc(l) + "</span>" : esc(l);
      }).join("\n");
    }

    // 用户翻到上面去看历史时别把他拽回底部
    if (stick) box.scrollTop = box.scrollHeight;

    var meta = [];

    if (d.size != null) meta.push("文件 " + fmtSize(d.size));
    meta.push("命中 " + (d.matched == null ? lines.length : d.matched) + " 行，显示 " + lines.length + " 行");
    if (d.truncated) meta.push("更早的行未显示");
    if (d.mtime) meta.push("更新于 " + new Date(d.mtime * 1000).toLocaleTimeString());
    if (d.reason) meta.push(d.reason);

    $("logMeta").textContent = meta.join(" · ");
  }

  function loadLogs() {
    var q = "?file=" + encodeURIComponent($("logFile").value || "")
      + "&level=" + encodeURIComponent($("logLevel").value)
      + "&lines=" + encodeURIComponent($("logLines").value);

    return get("api/panel/logs" + q).then(function (d) {
      renderLogFiles(d);
      renderLogBody(d);
    }).catch(function (e) {
      if (e.message !== "unauthorized") $("logBox").textContent = e.message;
    });
  }

  function stopLogAuto() {
    if (logTimer) { clearInterval(logTimer); logTimer = null; }
    $("logAuto").checked = false;
  }

  function startLogAuto() {
    if (logTimer) { clearInterval(logTimer); logTimer = null; }

    if (!$("logAuto").checked) return;

    logTimer = setInterval(loadLogs, 3000);
  }

  $("logAuto").onchange = startLogAuto;
  $("logBtn").onclick = function () { loadLogs().then(function () { toast("日志已刷新"); }); };

  $("logFile").onchange = loadLogs;
  $("logLevel").onchange = loadLogs;
  $("logLines").onchange = loadLogs;

  // ---- 云端同步：CD2 配置 + 排期备份 ----
  //
  // 备份不再走 docker exec 的逐文件复制，而是直连 CD2 的 gRPC 接口（见后端
  // util/clouddrive）：登录 → 按源目录找到它那条备份 → 让它重扫一遍。
  // 所以这一页现在要的是 CD2 的地址与账号，而不是容器名与两条文件系统路径。

  function renderSyncStatus(d) {
    var box = $("syncState");

    if (d.error) {
      box.innerHTML = "<b style='color:var(--text)'>CD2 状态：</b>" + esc(d.error);
      return;
    }

    var status = d.status;

    if (!status) {
      box.innerHTML = "<b style='color:var(--text)'>CD2 状态：</b>未取得";
      return;
    }

    var parts = ["<b style='color:var(--text)'>CD2 状态：</b>" + esc(status.label)];

    if (status.message) parts.push(esc(status.message));
    if (!status.enabled) parts.push("这条备份已停用");

    (status.destinations || []).forEach(function (dest) {
      var when = dest.last_finish
        ? new Date(dest.last_finish * 1000).toLocaleString()
        : "从未";

      parts.push("目标 " + esc(dest.path) + "（上次完成 " + esc(when) + "）");
    });

    box.innerHTML = parts.join(" · ");
  }

  function renderSyncConfig(d) {
    $("syncHost").value = d.host ? (d.host + ":" + (d.port || 19798)) : "";
    $("syncSource").value = d.source || "";
    $("syncUser").value = d.username || "";

    // 密码框永远留空：服务端只告诉我们"设过没有"，拿不到内容；
    // 空着提交 = 不改（服务端见 password 键缺席就保持原值）
    $("syncPass").value = "";
    $("syncPass").placeholder = d.has_password ? "已保存，留空不修改" : "";

    var note = [];

    if (d.saved) note.push("已保存自定义配置");

    if (d.has_password) note.push("账号密码已配置");
    else if (d.has_device_token) note.push("未配密码，将用 CD2 设备令牌");
    else note.push("还没配凭据，先填账号密码再保存");

    $("syncNote").textContent = note.join(" · ");

    renderSyncStatus(d);
    renderSyncPending(d.pending);
  }

  function loadSyncConfig() {
    $("syncErr").textContent = "";

    return get("api/panel/sync_config").then(renderSyncConfig).catch(function (e) {
      if (e.message !== "unauthorized") $("syncErr").textContent = e.message;
    });
  }

  $("syncReloadBtn").onclick = function () {
    loadSyncConfig().then(function () { toast("配置已重新载入"); });
  };

  $("syncSaveBtn").onclick = function () {
    var btn = this;
    var parts = ($("syncHost").value || "").trim().split(":");

    btn.disabled = true;
    $("syncErr").textContent = "";

    var config = {
      host: (parts[0] || "").trim(),
      port: (parts[1] || "19798").trim(),
      source: $("syncSource").value,
      username: $("syncUser").value
    };

    // 🔴 空密码框 = 不改：这个键干脆不带，服务端见它缺席就保持原值。
    // 每次都提交空串会把已存的密码抹掉 —— 服务端拿不到回显，本来也无从区分
    if ($("syncPass").value) config.password = $("syncPass").value;

    post("api/panel/sync_config/save", { config: config }).then(function (d) {
      renderSyncConfig(d);
      toast("云端同步配置已保存");
    }).catch(function (e) {
      if (e.message !== "unauthorized") $("syncErr").textContent = e.message;
    }).then(function () {
      btn.disabled = false;
    });
  };

  // 云端同步页那颗和概览页右上角那颗是同一个动作，实现收在这里共用。
  //
  // 两颗都不再立刻让 CD2 重扫：先记一笔排期，等下载队列彻底安静、再等设置里那个
  // 延迟（默认 5 分钟）才动手 —— 边下边传会把 FFmpeg 的中间产物和还没改名的文件
  // 一起传上去。要不要弹窗问一句由设置里的「同步前弹窗确认」决定（默认关）。
  //
  // 弹窗开关在服务端，所以点击时得先问一次配置；顺带把延迟分钟数取回来，好把
  // "还要等多久"写进提示 —— 否则用户点完只看到一句"已排期"，不知道在等什么。
  function runCloudBackup(btn) {
    var label = btn ? btn.textContent : "";

    if (btn) btn.disabled = true;

    get("api/panel/sync_config").then(function (d) {
      var minutes = d.delay_minutes || 0;

      var question = minutes
        ? "把下载目录同步到 115 网盘？会等当前下载任务全部完成，再等 " + minutes + " 分钟才开传。过程在后台执行，可随时回来看进度。"
        : "把下载目录同步到 115 网盘？会等当前下载任务全部完成后开传。过程在后台执行，可随时回来看进度。";

      if (d.confirm && !window.confirm(question)) return null;

      if (btn && btn.id === "syncBtn") btn.textContent = "☁ 排队中…";

      return post("api/panel/sync", {});
    }).then(function (d) {
      if (!d) return;

      toast(d.message || "已排期云端备份");
      renderSyncPending(d.pending);
    }).catch(function (e) {
      if (e.message !== "unauthorized") toast(e.message);
    }).then(function () {
      if (btn) {
        btn.disabled = false;
        if (label) btn.textContent = label;
      }
    });
  }

  // 排期状态那块。倒计时在前端走：服务端不为它开高频接口 —— 否则每秒都要越过
  // 一次登录鉴权，去问一个只存在于内存里的数
  var syncCountdown = null;

  function fmtRemaining(seconds) {
    seconds = Math.max(0, Math.round(seconds || 0));

    if (seconds < 60) return seconds + " 秒";

    var minutes = Math.floor(seconds / 60);
    var rest = seconds % 60;

    return minutes + " 分" + (rest ? " " + rest + " 秒" : "");
  }

  // 排期那次执行的结果。它是后台跑的，失败时用户不在场 —— 不把结果带回来，
  // 页面上就只剩"点过、等过、什么都没发生"
  function syncResultText(last) {
    if (!last) return "";

    var when = last.at ? new Date(last.at * 1000).toLocaleTimeString() : "";

    if (last.ok) return "　上次同步（" + esc(when) + "）：" + esc(last.message || "已触发");

    return "　上次同步（" + esc(when) + "）失败：" + esc(last.message || "原因没记下来");
  }

  function renderSyncPending(pending) {
    var box = $("syncPending");
    var cancel = $("syncCancelBtn");

    if (!box) return;

    if (syncCountdown) {
      clearInterval(syncCountdown);
      syncCountdown = null;
    }

    pending = pending || {};

    // 取消按钮只在真有排期时出现 —— 一直摆着会让人以为"排队中"是个常态
    if (cancel) cancel.hidden = !pending.armed;

    if (!pending.armed) {
      box.innerHTML = "<b style='color:var(--text)'>排期：</b>没有等待中的同步。"
        + "点上面那颗按钮后，面板会等下载任务全部跑完、再等 " + (pending.delay_minutes || 0)
        + " 分钟才让 CD2 重扫（延迟在设置页「云端同步」组里改）。"
        + syncResultText(pending.last_result);
      return;
    }

    if (pending.waiting_queue) {
      box.innerHTML = "<b style='color:var(--text)'>排期：</b>已排队 —— 正在等下载任务全部完成，"
        + "完成后再等 " + Math.round(pending.delay_minutes || 0) + " 分钟开始同步。"
        + "再点一次按钮可重新计时。" + syncResultText(pending.last_result);
      return;
    }

    var paint = function (left) {
      box.innerHTML = "<b style='color:var(--text)'>排期：</b>已排队 —— 下载队列已空，"
        + fmtRemaining(left) + "后开始同步。再点一次按钮可重新计时。"
        + syncResultText(pending.last_result);
    };

    var left = Math.max(0, Math.round(pending.seconds_left || 0));
    paint(left);

    syncCountdown = setInterval(function () {
      left -= 1;

      if (left <= 0) {
        clearInterval(syncCountdown);
        syncCountdown = null;
        box.innerHTML = "<b style='color:var(--text)'>排期：</b>已到时间，正在让 CD2 重扫…";
        return;
      }

      paint(left);
    }, 1000);
  }

  $("syncBtn").onclick = function () { runCloudBackup(this); };

  $("syncCancelBtn").onclick = function () {
    var btn = this;

    btn.disabled = true;

    post("api/panel/sync_cancel", {}).then(function (d) {
      toast(d.message || "已取消");
      renderSyncPending(d.pending);
    }).catch(function (e) {
      if (e.message !== "unauthorized") toast(e.message);
    }).then(function () {
      btn.disabled = false;
    });
  };

  // ---------------- 通知 ----------------

  var ntState = { loaded: false, config: {}, callbackPath: "/api/wecom/callback" };

  // ---- 卡片折叠：默认全部收起，用户展开过的记在 localStorage ----
  // 存的是"展开"而不是"收起"：以后往页面里加新卡片，天然就是收起的，不会漏改
  var NT_FOLD_KEY = "bili23.nt.fold";

  function ntFoldState() {
    try {
      return JSON.parse(localStorage.getItem(NT_FOLD_KEY) || "{}") || {};
    } catch (e) {
      return {};
    }
  }

  document.querySelectorAll("#page-notify .nt-item[data-nt]").forEach(function (card) {
    var key = card.getAttribute("data-nt");
    var head = card.querySelector(".h");

    card.classList.toggle("collapsed", ntFoldState()[key] !== true);

    head.addEventListener("click", function () {
      var collapsed = card.classList.toggle("collapsed");
      var now = ntFoldState();

      now[key] = !collapsed;

      try { localStorage.setItem(NT_FOLD_KEY, JSON.stringify(now)); } catch (e) {}
    });
  });

  // 收起来之后总得知道里面开没开、有多少条 —— 否则一排光标题等于没信息
  function ntBadge(id, on, onText, offText) {
    var el = $(id);

    if (!el) return;

    el.textContent = on ? (onText || "已启用") : (offText || "未启用");
    el.className = "ntbadge" + (on ? " on" : "");
  }

  // 凭据默认以 password 渲染，这个按钮只是给人核对用的 ——
  // 提交的始终是输入框里的值，与显示状态无关
  function ntBindEye(btnId, inputId) {
    $(btnId).onclick = function () {
      var input = $(inputId);
      var hidden = input.type === "password";

      input.type = hidden ? "text" : "password";
      this.textContent = hidden ? "隐藏" : "显示";
    };
  }

  // 凭据输入框一律**不复填真值** —— 服务端根本不下发（见 server 的 SECRET_KEYS）。
  // 留空提交表示"这一项不动"，所以要改就直接输入新的，别去改空框旁边的别的字段
  function ntFillSecret(id, isSet) {
    var input = $(id);

    input.value = "";
    input.placeholder = isSet ? "已设置 · 留空则不修改" : "未填写";
  }

  ntBindEye("ntWecomEye", "ntWecomSecret");
  ntBindEye("ntCbEye", "ntCbAes");
  ntBindEye("ntTgEye", "ntTgToken");

  // 对外地址一改，下面那条回调 URL 立刻跟着变 —— 用户复制的是"看得见的这条"，
  // 不能让他改完还得先保存才看到正确结果
  $("ntCbBase").addEventListener("input", ntRefreshCallbackUrl);

  $("ntCbCopy").onclick = function () {
    var url = $("ntCbUrl").value;

    if (!url) { toast("还没有可复制的地址"); return; }

    copyText(url);
  };

  // 发送记录与回调记录长得一样，只有"渠道"那一栏回调没有
  function ntRenderLogs(boxId, list, emptyText, badgeId) {
    var box = $(boxId);

    if (badgeId) {
      var count = (list || []).length;

      ntBadge(badgeId, count > 0, count + " 条", "暂无");
    }

    if (!list || !list.length) {
      box.innerHTML = '<div class="empty">' + emptyText + "</div>";
      return;
    }

    box.innerHTML = list.map(function (h) {
      return '<div class="ntlog">'
        + '<b class="' + (h.ok ? "ok" : "bad") + '">' + (h.ok ? "成功" : "失败") + "</b>"
        + (h.channel ? '<span class="ch">' + esc(h.channel) + "</span>" : "")
        + '<span class="tm">' + esc(h.time) + "</span>"
        + '<span class="dt">' + esc(h.detail) + "</span>"
        + "</div>";
    }).join("");
  }

  function ntRenderHistory(list) {
    ntRenderLogs("ntHistory", list, "还没有发送记录", "ntHistBadge");
  }

  function ntRender(d) {
    var c = d.config || {};
    var s = d.secrets || {};

    ntState.config = c;
    ntState.loaded = true;
    ntState.callbackPath = d.callback_path || "/api/wecom/callback";

    $("ntWecomOn").checked = !!c.wecom_enabled;
    $("ntWecomCorp").value = c.wecom_corp_id || "";
    $("ntWecomAgent").value = c.wecom_agent_id || "";
    ntFillSecret("ntWecomSecret", s.wecom_secret);
    // 服务端存的是企业微信要的 | 分隔形式，原样回填 —— 提交时逗号与竖线都认，
    // 所以来回一趟不会有改动
    $("ntWecomUser").value = c.wecom_touser || "";
    $("ntWecomBase").value = c.wecom_api_base || "";
    $("ntCbOn").checked = !!c.wecom_callback_enabled;
    ntFillSecret("ntCbToken", s.wecom_callback_token);
    ntFillSecret("ntCbAes", s.wecom_aes_key);
    $("ntCbBase").value = c.wecom_callback_base || "";
    $("ntTgOn").checked = !!c.telegram_enabled;
    ntFillSecret("ntTgToken", s.telegram_token);
    $("ntTgChat").value = c.telegram_chat_id || "";
    $("ntProxy").value = c.proxy || "";
    $("ntOnComplete").checked = !!c.on_complete;
    $("ntOnFail").checked = !!c.on_fail;

    // 标题栏上的状态胶囊：卡片收着也能一眼看出开没开
    ntBadge("ntWecomBadge", !!c.wecom_enabled);
    ntBadge("ntCbBadge", !!c.wecom_callback_enabled);
    ntBadge("ntTgBadge", !!c.telegram_enabled);
    ntBadge(
      "ntTrigBadge",
      !!(c.on_complete || c.on_fail),
      c.on_complete && c.on_fail ? "完成 + 失败" : (c.on_complete ? "仅完成" : "仅失败"),
      "都关闭"
    );

    ntRefreshCallbackUrl();
    ntRefreshCallbackNote(d);

    ntRenderHistory(d.history);
    ntRenderLogs("ntCallbackHistory", d.callback_history, "还没有收到过回调", "ntCbHistBadge");
  }

  // 回调地址 = 对外访问地址 + 路径。没填对外地址就用当前 origin ——
  // 从公网域名打开面板时这两者本来就一样
  function ntCallbackUrl() {
    var base = $("ntCbBase").value.trim().replace(/\/+$/, "");

    if (!base) base = location.origin;

    return base + (ntState.callbackPath || "/api/wecom/callback");
  }

  function ntRefreshCallbackUrl() {
    $("ntCbUrl").value = ntCallbackUrl();
  }

  // 回调这一栏没有"发送测试"可点（是企微来敲我们），所以状态只能由
  // 配置是否齐全 + 有没有收到过验证来判定
  function ntRefreshCallbackNote(d) {
    var msg = $("ntCbMsg");

    msg.className = "msg";

    if (!ntState.config.wecom_callback_enabled) {
      msg.textContent = "";
      return;
    }

    if (!d.callback_ready) {
      msg.className = "msg err";
      msg.textContent = "还差回调 Token 或 EncodingAESKey，补齐后保存才会生效";
      return;
    }

    var seen = (d.callback_history || []).some(function (h) {
      return h.ok && h.detail === "URL 验证通过";
    });

    if (seen) {
      msg.className = "msg ok";
      msg.textContent = "已经收到过合法的验证请求，链路是通的";
      return;
    }

    msg.textContent = "凭据已齐全，去企微后台填上面那条 URL 并保存，它验证通过后这里会变色";
  }

  // 提交的字段名要与 notify.normalize_settings 对得上 —— 服务端按同一份
  // 规则校验，这里不做重复判断
  function ntPayload() {
    return {
      wecom_enabled: $("ntWecomOn").checked,
      wecom_corp_id: $("ntWecomCorp").value.trim(),
      wecom_agent_id: $("ntWecomAgent").value.trim(),
      wecom_secret: $("ntWecomSecret").value.trim(),
      wecom_touser: $("ntWecomUser").value.trim(),
      wecom_api_base: $("ntWecomBase").value.trim(),
      wecom_callback_enabled: $("ntCbOn").checked,
      wecom_callback_token: $("ntCbToken").value.trim(),
      wecom_aes_key: $("ntCbAes").value.trim(),
      wecom_callback_base: $("ntCbBase").value.trim(),
      telegram_enabled: $("ntTgOn").checked,
      telegram_token: $("ntTgToken").value.trim(),
      telegram_chat_id: $("ntTgChat").value.trim(),
      proxy: $("ntProxy").value.trim(),
      on_complete: $("ntOnComplete").checked,
      on_fail: $("ntOnFail").checked
    };
  }

  function loadNotify(historyOnly) {
    $("ntErr").textContent = "";

    return get("api/panel/notify").then(function (d) {
      if (historyOnly) {
        // 只刷记录，不动输入框里正在编辑的内容。回调那两栏跟着一起刷 ——
        // "有没有被企微验证过"是这里最值得盯的一格
        ntState.callbackPath = d.callback_path || ntState.callbackPath;
        ntRenderHistory(d.history);
        ntRenderLogs("ntCallbackHistory", d.callback_history, "还没有收到过回调", "ntCbHistBadge");
        ntRefreshCallbackNote(d);
        return;
      }

      ntRender(d);
    }).catch(function (e) {
      if (e.message !== "unauthorized") $("ntErr").textContent = e.message;
    });
  }

  $("ntReloadBtn").onclick = function () {
    loadNotify(false).then(function () { toast("通知设置已重新载入"); });
  };

  $("ntSaveBtn").onclick = function () {
    var btn = this;

    btn.disabled = true;
    $("ntErr").textContent = "";

    post("api/panel/notify/save", { config: ntPayload() }).then(function (d) {
      ntRender(d);
      toast("通知设置已保存");
    }).catch(function (e) {
      if (e.message !== "unauthorized") $("ntErr").textContent = e.message;
    }).then(function () {
      btn.disabled = false;
    });
  };

  // 测试发送带上表单现值：用户多半是"填完先测一下，通了再保存"。
  // 发送失败服务端也回 200 + ok=false（那是结果不是错误），
  // 所以这里的 catch 只管真正的请求错误
  function ntTest(channel, btnId, msgId) {
    var btn = $(btnId);
    var msg = $(msgId);

    btn.disabled = true;
    btn.textContent = "发送中…";
    msg.className = "msg";
    msg.textContent = "";

    post("api/panel/notify/test", { channel: channel, config: ntPayload() }).then(function (d) {
      msg.className = "msg " + (d.ok ? "ok" : "err");
      msg.textContent = d.ok ? "已发出，去手机上看一眼" : d.message;

      ntRenderHistory(d.history);
    }).catch(function (e) {
      if (e.message !== "unauthorized") {
        msg.className = "msg err";
        msg.textContent = e.message;
      }
    }).then(function () {
      btn.disabled = false;
      btn.textContent = "发送测试";
    });
  }

  $("ntWecomTestBtn").onclick = function () {
    ntTest("wecom", "ntWecomTestBtn", "ntWecomTestMsg");
  };

  $("ntTgTestBtn").onclick = function () {
    ntTest("telegram", "ntTgTestBtn", "ntTgTestMsg");
  };

  // ---- 解析与建任务 ----

  // 一条条目行的 HTML。data-i 是它在 episodes 里的下标，勾选后按它取 id。
  // `on` 是默认勾不勾 —— 由下面「默认只勾正片」那段决定
  function epRow(ep, i, on) {
    return '<label class="ep"><input type="checkbox" data-i="' + i + '"'
      + (on ? " checked" : "") + ">"
      + '<span class="t" title="' + esc(ep.title) + '">' + esc(ep.title || "(无标题)") + "</span>"
      + '<span class="id">' + esc(ep.episode_id) + "</span></label>";
  }

  // 「正片」才是真正要下的那份内容，其余章节（相关推荐、精彩看点、高能片段、预告、花絮…）
  // 是一堆几分钟的切片。原来每条都默认勾着，一部 157 条的番里 145 条是切片 ——
  // 一路点「开始下载」就全拖下来了。
  //
  // 🔴 认不出「正片」时**保持全勾**（投稿视频那种平树、或者章节换了名字）：默认值宁可多勾，
  //    也不能因为认不出来就变成「一条都不下」，那更糟
  var MAIN_CHAPTER = "正片";

  function collectMainIds(node, into) {
    if (String(node.title || "").trim() === MAIN_CHAPTER) {
      (node.episode_ids || []).forEach(function (id) { into[id] = true; });
    }

    (node.children || []).forEach(function (child) { collectMainIds(child, into); });

    return into;
  }

  // 把 d.group 画成可折叠的分组：父行（类别/剧名、章节）带整组勾选框，条目缩进在下面。
  //
  // 桌面端那棵解析树就是这个形状。之前这里把所有条目摊成一片，一部 157 条的番剧里
  // 哪几条属于「正片」、哪几条是「相关推荐」，在面板上完全看不出来 —— 而它们该不该下
  // 差别很大。分组里只有 id，标题走 episodes 那条平表；被 limit 截掉的 id 直接跳过
  //
  // mainIds 为 null 表示「整棵树枝都默认勾」；否则只有落在那张表里的 id 默认勾上
  function groupRows(node, byId, mainIds) {
    var own = (node.episode_ids || []).filter(function (id) {
      return byId[id] !== undefined;
    });

    var kids = (node.children || []).map(function (child) {
      return groupRows(child, byId, mainIds);
    }).filter(function (s) { return s; });

    if (!own.length && !kids.length) return "";

    return '<div class="ggrp"><div class="ghead">'
      + '<button class="gtog" title="展开 / 收起" aria-label="展开或收起">▾</button>'
      + '<input type="checkbox" class="gchk" checked>'
      + (node.number ? '<span class="gnum">' + esc(node.number) + "</span>" : "")
      + '<span class="gttl">' + esc(node.title || node.number || "(未命名)") + "</span>"
      + '<span class="gcnt"></span></div><div class="gbody">'
      + kids.join("")
      + own.map(function (id) {
          return epRow(episodes[byId[id]], byId[id], !mainIds || mainIds[id] === true);
        }).join("")
      + "</div></div>";
  }

  // 勾选状态只有一份真源：条目行。父行的勾选框与「已选/总数」都由它推出来，
  // 免得两边各记一份、点几下就对不上
  function syncGroups() {
    var grps = $("episodes").querySelectorAll(".ggrp");

    for (var i = grps.length - 1; i >= 0; i--) {
      var boxes = grps[i].querySelectorAll('input[type="checkbox"][data-i]');
      var on = 0;

      boxes.forEach(function (b) { if (b.checked) on++; });

      var head = grps[i].querySelector(".gchk");

      head.checked = boxes.length > 0 && on === boxes.length;
      head.indeterminate = on > 0 && on < boxes.length;

      grps[i].querySelector(".gcnt").textContent = on + "/" + boxes.length;
    }
  }

  function renderGrouped(d) {
    episodes = (d.episodes || []).map(function (e) {
      return { episode_id: e.episode_id, title: e.title || e.leaf_title || e.episode_title || "" };
    });

    var byId = {};

    episodes.forEach(function (e, i) { byId[e.episode_id] = i; });

    // 整棵树里找一遍「正片」，找到几张 id 就默认勾这几张。一张都没找到（平树 / 章节换名）
    // 就留 null，退回「全勾」—— 见 MAIN_CHAPTER 那段的注释
    var mainIds = null;

    if (d.group) {
      var main = collectMainIds(d.group, {});

      if (Object.keys(main).length) mainIds = main;
    }

    var html = d.group ? groupRows(d.group, byId, mainIds) : "";

    // 分组缺席（或整棵树都没条目）时退回平铺，总不能让面板空着。
    // 平铺时没有章节信息，只能全勾
    if (!html) {
      html = episodes.map(function (e, i) { return epRow(e, i, true); }).join("");
    }

    $("episodes").innerHTML = html;

    // 一次解析给的条目有上限（500），超出的在面板上就画不出来 —— 说清楚还有多少，
    // 别让人以为这部番只有这么多集
    $("parseCount").innerHTML = "<b>" + Number(episodes.length) + "</b> 个条目"
      + (Number(d.total) > episodes.length ? "（共 " + Number(d.total) + " 项）" : "");

    syncGroups();
  }

  // 分组每次解析都重建，逐个绑不如在容器上接一次
  $("episodes").addEventListener("change", function (e) {
    if (e.target.classList.contains("gchk")) {
      // 父行勾选框：整组跟着走，嵌套的子组一并覆盖
      e.target.closest(".ggrp")
        .querySelectorAll('input[type="checkbox"][data-i]')
        .forEach(function (b) { b.checked = e.target.checked; });
    }

    syncGroups();
  });

  $("episodes").addEventListener("click", function (e) {
    var tog = e.target.closest(".gtog");

    if (!tog) return;

    var grp = tog.closest(".ggrp");

    grp.classList.toggle("collapsed");
    tog.textContent = grp.classList.contains("collapsed") ? "▸" : "▾";
  });

  // 同系列还有别的季时给一个下拉，换一项就重新解析那一部。
  // 只有一季等于没得选，那就别占着位置（桌面端也是 ≥1 就显示，这里更省地方）
  function renderSeasonSel(d, url) {
    var sel = $("seasonSel");
    var list = d.seasons || [];

    if (list.length < 2) {
      sel.classList.add("hidden");
      sel.innerHTML = "";
      return;
    }

    sel.innerHTML = list.map(function (s) {
      // 优先按 B站季号认（链接可能给的是 ep 而不是 ss，光比 URL 会一个都不选中）
      var current = (d.current_season_id != null && s.season_id === d.current_season_id)
        || s.url === url;

      return '<option value="' + esc(s.url) + '"' + (current ? " selected" : "") + ">"
        + esc(s.title || s.url) + "</option>";
    }).join("");

    sel.classList.remove("hidden");
  }

  // 铺开一次解析结果。单条解析与批量解析都落到这里
  function showParsed(d, url) {
    renderGrouped(d);
    renderSeasonSel(d, url || "");
    $("parseList").classList.remove("hidden");
  }

  // 解析一个链接并把条目按分组铺开。点「解析」与换季下拉都走这里
  function runParse(url, page) {
    $("parseMsg").textContent = "";
    $("parseList").classList.add("hidden");

    // 一次要 500 条（工具允许的上限）。默认只给 100，而分组是按完整列表画的，
    // 少掉的那一截在面板上会整段消失
    return api("parse_url", { url: url, page: page || 1, limit: 500 }).then(function (d) {
      if (!(d.episodes || []).length) throw new Error("没有解析到任何条目");

      showParsed(d, url);
      toast("解析到 " + episodes.length + " 个条目");

      return d;
    });
  }

  function parseFailed(e) {
    if (e.message !== "unauthorized") $("parseMsg").textContent = e.message;
  }

  $("parseBtn").onclick = function () {
    var url = $("url").value.trim();
    if (!url) { toast("请先粘贴链接"); return; }

    var btn = this;
    btn.disabled = true;

    runParse(url, 1).catch(parseFailed).then(function () {
      btn.disabled = false;
    });
  };

  $("seasonSel").onchange = function () {
    var url = this.value;
    if (!url) return;

    var sel = this;
    sel.disabled = true;

    // 地址栏跟着改：不然换了季以后看不出现在看的是哪一部
    $("url").value = url;

    runParse(url, 1).catch(parseFailed).then(function () {
      sel.disabled = false;
    });
  };

  // 回车直接解析，跟点「解析」一个效果
  $("url").addEventListener("keydown", function (e) {
    if (e.key === "Enter") $("parseBtn").click();
  });

  function fmtWhen(ts) {
    return ts ? new Date(ts * 1000).toLocaleString() : "—";
  }

  // ---- 批量解析（桌面端在「解析」按钮的下拉里）----

  function batchLines() {
    return $("batchText").value.split("\n")
      .map(function (s) { return s.trim(); })
      .filter(function (s) { return s; });
  }

  // 🔴 那个「自动加入下载列表」复选框的初值必须来自用户的当前设置，不能从 DOM 的
  // 默认未勾选起步。批量工具写的就是全局配置（桌面端那个对话框也写它），于是
  // "永远提交 false" = **每点一次批量解析就把用户开着的开关关一次**，且毫无提示。
  // 读设置失败时不提交这个字段（工具侧：非布尔 = 不动设置）—— 宁可少同步一次，
  // 也别把用户的开关悄悄改掉
  var batchAutoKnown = false;

  $("batchBtn").onclick = function () {
    $("batchErr").textContent = "";
    $("batchCount").textContent = "已填 0 条";
    $("batchGate").classList.remove("hidden");
    $("batchText").focus();

    batchAutoKnown = false;
    $("batchAuto").checked = false;

    get("api/panel/settings").then(function (d) {
      var field = (d.fields || []).filter(function (f) {
        return f.key === "auto_add_to_download_list";
      })[0];

      // 只认真正的布尔值：读失败时后端给的是 null，拿它当"关着"照样会把开关改掉
      if (field && typeof field.value === "boolean") {
        $("batchAuto").checked = field.value;
        batchAutoKnown = true;
      }
    }).catch(function () { /* 读不到就保持"不提交" */ });
  };

  // 条数实时报出来：批量是逐条串行跑的，链接贴错一条要等到整批跑完才发现太亏
  $("batchText").oninput = function () {
    $("batchCount").textContent = "已填 " + batchLines().length + " 条";
  };

  $("batchCancel").onclick = function () {
    $("batchGate").classList.add("hidden");
  };

  $("batchOk").onclick = function () {
    var lines = batchLines();
    var btn = this;

    if (!lines.length) {
      $("batchErr").textContent = "请先粘贴至少一条链接";
      return;
    }

    $("batchErr").textContent = "";
    btn.disabled = true;
    btn.textContent = "解析中…";

    // 读不到设置就不带 auto_add：不传 = 按用户原来的设置来（见 batchAutoKnown 的说明）
    var body = { urls: lines, limit: 500 };

    if (batchAutoKnown) body.auto_add = $("batchAuto").checked;

    // 一次要的 500 条与单条解析同理：分组按完整列表画，少了那截会整段消失
    api("parse_batch", body)
      .then(function (d) {
        $("batchGate").classList.add("hidden");

        showParsed(d, "");

        // 地址栏跟到第一条：批量跑完接着换季、或者单独再解析，都从这儿续
        $("url").value = lines[0];

        toast("已解析 " + Number(d.parsed_links || lines.length) + " 条链接，列表共 " + Number(d.total) + " 个条目");
      })
      .catch(function (e) { $("batchErr").textContent = e.message; })
      .then(function () {
        btn.disabled = false;
        btn.textContent = "开始解析";
      });
  };

  // ---- 解析记录（桌面端解析页上方那颗时钟图标）----

  function loadHistory() {
    var box = $("histList");

    $("histErr").textContent = "";
    box.innerHTML = '<div class="muted" style="padding:10px 2px">加载中…</div>';

    api("list_parse_history", {}).then(function (d) {
      var list = d.records || [];

      $("histCount").innerHTML = "<b>" + Number(d.total) + "</b> 条";

      if (!list.length) {
        box.innerHTML = '<div class="muted" style="padding:10px 2px">还没有记录。桌面端设置里关掉解析历史后就不会再记了。</div>';
        return;
      }

      box.innerHTML = list.map(function (r, i) {
        return '<div class="hrow" data-id="' + esc(r.history_id) + '">'
          + '<span class="hidx">' + (i + 1) + "</span>"
          + '<span class="httl" title="' + esc(r.url) + '">' + esc(r.title || "(无标题)") + "</span>"
          + '<span class="htyp">' + esc(r.type_label || r.type) + "</span>"
          + '<span class="htime">' + esc(fmtWhen(r.created_time)) + "</span>"
          + '<button class="sm hparse">解析</button>'
          + '<button class="sm hdel">删除</button>'
          + "</div>";
      }).join("");

      // 逐行取回这一条记录再绑事件：历史 id 会被拼进 HTML，用列表里的原对象
      // 比从 dataset 反查更稳
      box.querySelectorAll(".hrow").forEach(function (row) {
        var record = list.filter(function (r) { return r.history_id === row.dataset.id; })[0];

        if (!record) return;

        row.querySelector(".hparse").onclick = function () {
          if (!record.url) { toast("这条记录没有可解析的地址"); return; }

          $("histGate").classList.add("hidden");
          $("url").value = record.url;

          runParse(record.url, 1).catch(parseFailed);
        };

        row.querySelector(".hdel").onclick = function () {
          api("delete_parse_history", { history_id: record.history_id })
            .then(loadHistory)
            .catch(function (e) { $("histErr").textContent = e.message; });
        };
      });
    }).catch(function (e) {
      box.innerHTML = "";
      $("histErr").textContent = e.message;
    });
  }

  $("histBtn").onclick = function () {
    $("histGate").classList.remove("hidden");
    loadHistory();
  };

  $("histClose").onclick = function () {
    $("histGate").classList.add("hidden");
  };

  $("histClear").onclick = function () {
    // 不可逆，先问一句。桌面端那个「清除历史」是点了就清，但这里隔着浏览器，
    // 误触的代价一样，多问一句不多余
    if (!window.confirm("清空全部解析记录？此操作不可撤销。")) return;

    var btn = this;

    btn.disabled = true;

    api("clear_parse_history", {})
      .then(loadHistory)
      .catch(function (e) { $("histErr").textContent = e.message; })
      .then(function () { btn.disabled = false; });
  };

  // 只认条目行那一层：分组头的 .gchk 没有 data-i，混进来会把整组当成一个条目
  function leafBoxes() {
    return $("episodes").querySelectorAll('input[type="checkbox"][data-i]');
  }

  $("allBtn").onclick = function () {
    var boxes = leafBoxes();
    var all = boxes.length > 0 && Array.prototype.every.call(boxes, function (b) { return b.checked; });

    Array.prototype.forEach.call(boxes, function (b) { b.checked = !all; });

    syncGroups();
  };

  $("downBtn").onclick = function () {
    var picked = [];
    leafBoxes().forEach(function (b) {
      if (b.checked) picked.push(episodes[Number(b.dataset.i)].episode_id);
    });

    if (!picked.length) { toast("请至少勾选一个条目"); return; }

    var btn = this;
    btn.disabled = true;

    api("create_download", { episode_ids: picked }).then(function () {
      toast("已创建 " + picked.length + " 个下载任务");
      $("parseList").classList.add("hidden");
      $("url").value = "";
      episodes = [];

      // 建完任务直接带去「下载中」页盯进度
      return refresh().then(function () { showPage("downloading"); });
    }).catch(function (e) {
      if (e.message !== "unauthorized") toast(e.message);
    }).then(function () {
      btn.disabled = false;
    });
  };

  $("refreshBtn").onclick = function () {
    var btn = this;

    // 转一圈当作"正在取数"的反馈；取数是本地的，很快，所以固定转 0.7s 就停
    btn.classList.add("spin");
    setTimeout(function () { btn.classList.remove("spin"); }, 700);

    refresh();
    refreshLogin();
    loadSettings();
    toast("已刷新");
  };

  $("ovBackupBtn").onclick = function () { runCloudBackup(this); };

  function startPolling() {
    if (timer) clearInterval(timer);
    timer = setInterval(refresh, 3000);
  }

  function boot() {
    // 先问一次会话：能答上来就说明 Cookie 还有效，否则上面的 401 已经把人送回登录框
    post("api/panel/session").then(function (d) {
      $("gate").classList.add("hidden");
      refreshLogin();
      loadSettings();
      refresh().then(startPolling);
    }).catch(function () {});
  }

  // ---- 账号收藏 ----
  //
  // 三份列表（收藏夹 / 订阅合集 / 追番追剧）都是只读的；点开一张卡片才去解析
  // 那个收藏夹的内容，而解析走的就是「下载中」页同一条链路（parse_url）——
  // 这里不重写任何解析或建任务的逻辑。
  //
  // 每次进页面 / 点刷新都重新取：收藏夹在手机上随时会变，留着旧数据等于骗人。

  var favState = { kind: "", busy: false };

  function favSetKind(kind) {
    favState.kind = kind;

    document.querySelectorAll("#favTabs button").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-fav") === kind);
    });

    loadFavorites();
  }

  function loadFavorites() {
    var kind = favState.kind || "favorite";
    var box = $("favList");

    favState.busy = true;
    box.innerHTML = '<div class="empty">加载中…</div>';
    $("favCount").innerHTML = "";

    get("api/panel/favorites?kind=" + encodeURIComponent(kind)).then(function (d) {
      // 稍后再看 / 历史记录没有列表接口：服务端只回一句"该解析哪个地址"。
      // 这两类的地址写死在服务端（favorites.DIRECT_PARSE_KINDS），前端不认 URL
      if (d.ok && d.direct_parse) { favParseDirect(d); return; }

      renderFavorites(d);
    }).catch(function (e) {
      if (e.message !== "unauthorized") {
        box.innerHTML = '<div class="empty">' + esc(e.message) + "</div>";
      }
    }).then(function () {
      favState.busy = false;
    });
  }

  function favSub(e) {
    var parts = [];

    if (e.count) parts.push(e.count + " 个内容");
    if (e.desc) parts.push(e.desc);

    return parts.join(" · ") || "点开查看";
  }

  function renderFavorites(d) {
    var box = $("favList");

    if (!d.ok) {
      // 没登录是最常见的一种：直接给一个能走的下一步，而不是干巴巴一句错误
      box.innerHTML = '<div class="empty">' + esc(d.error || "没取到收藏列表")
        + (d.need_login ? '　<a href="javascript:showPage(\'account\')">去登录</a>' : "")
        + "</div>";
      return;
    }

    var list = d.entries || [];

    $("favCount").innerHTML = "<b>" + list.length + "</b> 个" + esc(d.label || "收藏");

    if (!list.length) {
      box.innerHTML = '<div class="empty">这个分类下还没有内容</div>';
      return;
    }

    box.innerHTML = list.map(function (e, i) {
      // 首字垫在下面、封面盖在上面：收藏夹接口本身不返回封面，
      // 而封面又来自外网（NAS 上的浏览器未必出得去）—— 两条路都要有个像样的样子
      var face = "<b>" + esc((e.title || "?").slice(0, 1)) + "</b>"
        + (e.cover
          ? '<img src="' + esc(e.cover) + '" alt="" loading="lazy" onerror="this.remove()">'
          : "");

      return '<div class="favcard" data-i="' + i + '">'
        + '<div class="fc">' + face + "</div>"
        + '<div class="ft"><b title="' + esc(e.title) + '">' + esc(e.title || "(无标题)") + "</b>"
        + "<span>" + esc(favSub(e)) + "</span></div>"
        + "</div>";
    }).join("");

    box.querySelectorAll(".favcard").forEach(function (card) {
      card.onclick = function () { toggleFavorite(list[Number(card.dataset.i)], card); };
    });
  }

  function toggleFavorite(entry, card) {
    var next = card.nextElementSibling;

    // 再点一次就收起（展开块永远紧跟在这张卡片后面）
    if (next && next.classList.contains("favep")) {
      next.remove();
      return;
    }

    // 同时只展开一个：不然一屏里堆着好几份勾选列表，分不清在挑哪个
    $("favList").querySelectorAll(".favep").forEach(function (b) { b.remove(); });

    var panel = document.createElement("div");

    panel.className = "favep";
    card.parentNode.insertBefore(panel, card.nextSibling);

    // 解析结果与「下载中」页那套完全一致（episodes[].episode_id），
    // 所以建任务直接复用 create_download
    favParseInto(panel, entry, 1, entry.url);
  }

  // 「稍后再看」「历史记录」桌面端就不出列表 —— 点一下直接把自定义地址丢给解析器
  // （flyout.py:246,253）。这里照做：没有卡片，直接铺一整行条目
  function favParseDirect(d) {
    var box = $("favList");
    var panel = document.createElement("div");

    panel.className = "favep";
    box.innerHTML = "";
    box.appendChild(panel);

    favParseInto(panel, { title: d.label || "", direct: true }, 1, d.parse_url);
  }

  // 解析一个地址并把条目画进 panel —— 收藏夹展开、稍后再看、历史记录三处共用。
  // url 单独传而不是从 meta 上取：换季以后 meta.url 不该被改掉，
  // 否则收起再展开会跑去解析另一个季，而用户以为自己看的是原来那个
  function favParseInto(panel, meta, page, url) {
    panel.innerHTML = '<div class="bar"><span class="t">正在解析…</span></div>';

    api("parse_url", { url: url, page: page || 1 }).then(function (d) {
      favRenderEpisodes(panel, meta, d, url);
    }).catch(function (e) {
      if (e.message === "unauthorized") return;

      // 这两类都要登录态。未登录时解析器抛的就是「请先登录」，
      // 顺手给一个能走的下一步，比让人对着错误对象猜强
      var need = /登录|log ?in/i.test(e.message);

      panel.innerHTML = '<div class="bar"><span class="t">' + esc(e.message)
        + (need ? '　<a href="javascript:showPage(\'account\')">去登录</a>' : "")
        + "</span></div>";
    });
  }

  // 同系列还有别的季时给一个下拉 —— 就是桌面端解析页左下角那个能"解析好几部"的东西。
  // 只有一季等于没得选，不占位置
  function favSeasonSel(d, url) {
    var list = d.seasons || [];

    if (list.length < 2) return "";

    return '<select class="favseason" data-act="season">' + list.map(function (s) {
      // 优先按 B站季号认：链接给的是 ep 时，光比 URL 会一个都选不中
      var current = (d.current_season_id != null && s.season_id === d.current_season_id)
        || s.url === url;

      return '<option value="' + esc(s.url) + '"' + (current ? " selected" : "") + ">"
        + esc(s.title || s.url) + "</option>";
    }).join("") + "</select>";
  }

  function favRenderEpisodes(panel, meta, d, url) {
    var list = d.episodes || [];
    var page = (d.pagination || {}).current_page || 1;
    var pages = (d.pagination || {}).total_pages || 1;
    var total = (d.pagination || {}).total_items;

    $("favCount").innerHTML = "<b>" + list.length + "</b> 个条目";

    if (!list.length) {
      // 空态要说清是"哪个东西"空的：稍后再看 / 历史记录本来就可能是空的
      // （账号里没存过东西），那不是异常；收藏夹解析不出内容才是
      panel.innerHTML = '<div class="bar"><span class="t">'
        + (meta.direct ? "这个列表里暂时没有内容" : "这个收藏夹没解析出内容")
        + "</span></div>";
      return;
    }

    panel.innerHTML = '<div class="bar">'
      + '<span class="t">' + esc(meta.title || "") + " · " + list.length + " 个条目"
      + (pages > 1
        ? "（第 " + page + "/" + pages + " 页" + (total ? " · 共 " + total + " 个" : "") + "）"
        : "")
      + "</span>"
      + favSeasonSel(d, url)
      + '<span class="spacer"></span>'
      // 翻页只对"接口本身分页"的那两类有意义（稍后再看 / 历史记录）；
      // 翻页会重新解析，等于整页替换，所以勾选跟着重来 —— 页码里已经写明了在第几页
      + (page > 1 ? '<button class="sm" data-act="prev">上一页</button>' : "")
      + (page < pages ? '<button class="sm" data-act="next">下一页</button>' : "")
      + '<button class="sm" data-act="all">全选 / 反选</button>'
      + '<button class="sm primary" data-act="down">下载选中</button>'
      + "</div>"
      + '<div class="eps">' + list.map(function (e, i) {
          return '<label class="ep"><input type="checkbox" data-i="' + i + '" checked>'
            + '<span class="t" title="' + esc(e.title) + '">' + esc(e.title || "(无标题)") + "</span>"
            + '<span class="id">' + esc(e.episode_id) + "</span></label>";
        }).join("") + "</div>";

    var boxes = panel.querySelectorAll(".eps input");

    panel.querySelector('[data-act="all"]').onclick = function () {
      var all = Array.prototype.every.call(boxes, function (b) { return b.checked; });

      Array.prototype.forEach.call(boxes, function (b) { b.checked = !all; });
    };

    panel.querySelector('[data-act="down"]').onclick = function () {
      var picked = [];

      Array.prototype.forEach.call(boxes, function (b) {
        if (b.checked) picked.push(list[Number(b.dataset.i)].episode_id);
      });

      if (!picked.length) { toast("请至少勾选一个条目"); return; }

      var btn = this;
      btn.disabled = true;

      api("create_download", { episode_ids: picked }).then(function () {
        toast("已创建 " + picked.length + " 个下载任务");
        panel.remove();

        // 建完任务直接带去「下载中」页盯进度，与解析页的收尾一致
        return refresh().then(function () { showPage("downloading"); });
      }).catch(function (e) {
        if (e.message !== "unauthorized") toast(e.message);
      }).then(function () {
        btn.disabled = false;
      });
    };

    // 换季：重新解析那一部，当前的勾选一并作废（都是另一部的内容了）
    var season = panel.querySelector('[data-act="season"]');

    if (season) {
      season.onchange = function () { favParseInto(panel, meta, 1, this.value); };
    }

    var prev = panel.querySelector('[data-act="prev"]');

    if (prev) {
      prev.onclick = function () { favParseInto(panel, meta, page - 1, url); };
    }

    var next = panel.querySelector('[data-act="next"]');

    if (next) {
      next.onclick = function () { favParseInto(panel, meta, page + 1, url); };
    }
  }

  document.querySelectorAll("#favTabs button").forEach(function (b) {
    b.onclick = function () { favSetKind(b.getAttribute("data-fav")); };
  });

  $("favReloadBtn").onclick = function () { loadFavorites(); };

  boot();
})();
</script>
</body>
</html>
"""
