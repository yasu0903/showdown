#!/usr/bin/env python3
"""Battle log analyzer: HTML → JSON + Markdown using Gemini API"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import google.generativeai as genai

PLAYER_NAME = "yasu1000"
MODEL_NAME = "gemini-3-flash-preview"
ARCHETYPES = ["トリル", "晴れ", "雨", "雪", "砂", "テールウインド", "積みサポート", "スタン"]

PROMPT = """\
以下はPokemon Showdownのダブルバトル（VGC）の対戦ログです。
分析対象プレイヤー: {player_name}

--- 対戦ログ ---
{battle_log}
---

下記のJSONスキーマで分析結果を返してください。JSONのみ出力し、説明文は不要です。

{{
  "result": "win" または "lose",
  "turns_total": 総ターン数（整数）,
  "my_lead": ["先発1", "先発2"],
  "my_selected": ["選出した4体の英語名（登場順）"],
  "win_condition": "勝因を一言で（日本語）",
  "lose_condition": "敗因を一言で（日本語）",
  "opponent": {{
    "name": "相手プレイヤー名",
    "party": ["全6体の英語名"],
    "selected": ["選出した4体の英語名（登場順）"],
    "lead": ["相手の先発2体の英語名"],
    "archetype": "{archetypes} のいずれか1つ",
    "key_pokemon": ["相手構築の主軸ポケモン（1〜2体）の英語名"],
    "strategy": "相手の戦略を一言で（日本語）"
  }},
  "key_moments": [
    {{
      "turn": ターン番号（整数）,
      "matchup": ["関与した主要ポケモンの英語名（1〜2体）"],
      "board_state": "そのターン開始時の場の状態（残数・HP・状態異常など、日本語、1文）",
      "my_action": "{player_name} が取った行動（日本語、1文）",
      "opponent_action": "相手が取った行動（日本語、1文）",
      "note": "なぜその場面が重要だったか（日本語、1〜2文）"
    }}
  ],
  "summary": "試合全体の流れと勝因・敗因（日本語、3〜5文）",
  "title": "試合タイトル（例：VSアーカルゴンスタン 初手から主導権を握る）"
}}

## 補足
- win_condition / lose_condition：結果が win なら lose_condition は null、lose なら win_condition は null にする
- archetype の判断基準
  - トリル：トリックルームを展開軸とする
  - 晴れ・雨・雪・砂：該当天候を軸とする
  - テールウインド：テール風でのスピードコントロールを軸とする
  - 積みサポート：フォローミー・いかりのこな等のサポートと積み技の組み合わせを軸とする
  - スタン：上記に当てはまらないバランス型
- key_moments の選び方
  - 択の正誤を評価するのではなく、強い意思決定が必要だった場面を選ぶ
  - ポケモンの対面が試合の流れを左右した場面を選ぶ
  - 1試合につき1〜3個
"""


def extract_log_data(html_path: Path) -> str:
    content = html_path.read_text(encoding="utf-8")
    m = re.search(
        r'<script type="text/plain" class="battle-log-data">(.*?)</script>',
        content,
        re.DOTALL,
    )
    if not m:
        raise ValueError(f"battle-log-data not found: {html_path.name}")
    return m.group(1).strip()


def extract_date(stem: str) -> str:
    m = re.search(r"(\d{4}-\d{2}-\d{2})", stem)
    return m.group(1) if m else ""


def call_llm(log_data: str, model) -> dict:
    prompt = PROMPT.format(
        player_name=PLAYER_NAME,
        battle_log=log_data,
        archetypes="・".join(ARCHETYPES),
    )
    response = model.generate_content(
        prompt,
        generation_config=genai.GenerationConfig(
            response_mime_type="application/json",
        ),
    )
    return json.loads(response.text)


def to_markdown(data: dict, html_name: str) -> str:
    result_label = "勝利" if data["result"] == "win" else "敗北"
    opp = data["opponent"]
    my_selected = " / ".join(data.get("my_selected", []))
    my_lead = " / ".join(data["my_lead"])
    condition = data.get("win_condition") or data.get("lose_condition") or ""
    condition_label = "勝因" if data["result"] == "win" else "敗因"

    lines = [
        f"# {data['title']}",
        "",
        f"**結果**：{result_label}　**{condition_label}**：{condition}  ",
        f"**相手**：{opp['name']}　**構築タイプ**：{opp['archetype']}  ",
        f"**相手の戦略**：{opp.get('strategy', '')}  ",
        f"**相手の主軸**：{' / '.join(opp.get('key_pokemon', []))}  ",
        "",
        "## 選出",
        "",
        "| | 6体 | 選出 | 先発 |",
        "|---|---|---|---|",
        f"| 自分 | — | {my_selected} | {my_lead} |",
        f"| 相手 | {' / '.join(opp['party'])} | {' / '.join(opp['selected'])} | {' / '.join(opp['lead'])} |",
        "",
        "## 試合概要",
        "",
        data["summary"],
        "",
        "## 分岐点",
        "",
    ]

    for km in data["key_moments"]:
        matchup = " vs ".join(km["matchup"])
        lines += [
            f"### ターン {km['turn']} — {matchup}",
            "",
            f"**場の状態**：{km.get('board_state', '')}  ",
            f"**自分の行動**：{km.get('my_action', '')}  ",
            f"**相手の行動**：{km.get('opponent_action', '')}  ",
            f"**重要な理由**：{km['note']}  ",
            "",
        ]

    lines += [
        "---",
        "",
        f"[元の対戦ログ](./{html_name})",
    ]
    return "\n".join(lines)


def process_logs_dir(logs_dir: Path, model, force: bool = False) -> list:
    results = []
    for html_path in sorted(logs_dir.glob("*.html")):
        json_path = html_path.with_suffix(".json")
        md_path = html_path.with_suffix(".md")

        if json_path.exists() and not force:
            results.append(json.loads(json_path.read_text(encoding="utf-8")))
            print(f"  skip (分析済み): {html_path.name}")
            continue

        print(f"  analyzing: {html_path.name} ...", end=" ", flush=True)
        try:
            log_data = extract_log_data(html_path)
            data = call_llm(log_data, model)
            data["file"] = html_path.stem
            data["date"] = extract_date(html_path.stem)

            json_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            md_path.write_text(to_markdown(data, html_path.name), encoding="utf-8")
            results.append(data)
            print(f"→ {data['title']}")
        except Exception as e:
            print(f"ERROR: {e}")

    return results


def regenerate_index(deck_dir: Path, results: list):
    if not results:
        return

    index_path = deck_dir / "index.md"
    existing = index_path.read_text(encoding="utf-8") if index_path.exists() else ""

    header = existing.split("# ログ")[0].rstrip() if "# ログ" in existing else existing.rstrip()

    wins = sum(1 for r in results if r["result"] == "win")
    losses = len(results) - wins

    log_lines = ["", "", "# ログ", ""]
    for r in sorted(results, key=lambda x: x.get("date", "")):
        mark = "○" if r["result"] == "win" else "×"
        md_name = r["file"] + ".md"
        log_lines.append(f"[{mark} {r['title']}](./logs/{md_name})  ")

    log_lines += ["", f"**戦績**：{wins}勝{losses}敗", ""]

    index_path.write_text(header + "\n".join(log_lines), encoding="utf-8")
    print(f"  → index.md 更新（{wins}勝{losses}敗）")


def main():
    parser = argparse.ArgumentParser(
        prog="analyze.py",
        description=(
            "Pokemon Showdown のダブルバトル（VGC）対戦ログ（HTML）を\n"
            "Gemini API で解析し、JSON と Markdown を生成します。\n\n"
            "対象ディレクトリに含まれる全 HTML ファイルを処理し、\n"
            "同名の .json / .md を出力します。すでに .json が存在する場合はスキップ。\n"
            "処理後、デッキディレクトリ直下の index.md を自動更新します。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "使用例:\n"
            "  python analyze.py decks/               # decks/ 配下の全 logs/ を処理\n"
            "  python analyze.py decks/myteam/logs/   # 特定の logs/ を処理\n\n"
            "環境変数:\n"
            "  GEMINI_API_KEY   Gemini API キー（必須）"
        ),
    )
    parser.add_argument(
        "target_dir",
        metavar="<target_dir>",
        help=(
            "処理対象のディレクトリ。"
            "logs/ ディレクトリを直接指定するか、"
            "その親ディレクトリを指定すると配下の全 logs/ を再帰的に処理します。"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="既存の JSON・MD があっても上書きして再生成する。",
    )
    parser.add_argument(
        "--model",
        default=MODEL_NAME,
        metavar="<model>",
        help=f"使用する Gemini モデル名。デフォルト: {MODEL_NAME}",
    )
    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY が設定されていません", file=sys.stderr)
        sys.exit(1)

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(args.model)
    print(f"model: {args.model}")

    target = Path(args.target_dir).resolve()
    if not target.is_dir():
        print(f"Error: ディレクトリが見つかりません: {target}", file=sys.stderr)
        sys.exit(1)

    logs_dirs = [target] if target.name == "logs" else list(target.glob("**/logs"))
    logs_dirs = [d for d in logs_dirs if d.is_dir()]

    for logs_dir in sorted(logs_dirs):
        print(f"\n{logs_dir}")
        results = process_logs_dir(logs_dir, model, force=args.force)
        if results:
            regenerate_index(logs_dir.parent, results)


if __name__ == "__main__":
    main()
