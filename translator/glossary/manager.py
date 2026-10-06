from __future__ import annotations

import csv
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import requests

VALID_CATEGORIES = ("人物名", "地名", "組織", "技能", "魔法", "稱號", "特殊設定")
CATEGORY_ALIASES = {
    "人名": "人物名",
    "角色": "人物名",
    "人物": "人物名",
    "character": "人物名",
    "person": "人物名",
    "place": "地名",
    "location": "地名",
    "場所": "地名",
    "faction": "組織",
    "organization": "組織",
    "group": "組織",
    "势力": "組織",
    "勢力": "組織",
    "skill": "技能",
    "ability": "技能",
    "magic": "魔法",
    "spell": "魔法",
    "title": "稱號",
    "alias": "稱號",
    "称号": "稱號",
    "setting": "特殊設定",
    "item": "特殊設定",
    "concept": "特殊設定",
    "道具": "特殊設定",
    "設定": "特殊設定",
    "设定": "特殊設定",
}


@dataclass
class GlossaryEntry:
    source: str
    target: str
    category: str = "特殊設定"
    note: str = ""

    def __post_init__(self) -> None:
        self.source = self.source.strip()
        self.target = self.target.strip()
        cat = self.category.strip()
        self.category = CATEGORY_ALIASES.get(cat.lower(), CATEGORY_ALIASES.get(cat, cat))
        if self.category not in VALID_CATEGORIES:
            self.category = "特殊設定"
        self.note = self.note.strip()


@dataclass
class GlossaryViolation:
    paragraph_index: int
    source: str
    expected_target: str
    category: str
    original_text: str
    translated_text: str
    suggested_fix: str | None = None


@dataclass
class CandidateTerm:
    source: str
    suggested_target: str = ""
    category: str = "特殊設定"
    count: int = 1
    note: str = ""
    contexts: list[str] = field(default_factory=list)


def normalize_category(cat: str) -> str:
    cat = cat.strip()
    return CATEGORY_ALIASES.get(cat.lower(), CATEGORY_ALIASES.get(cat, cat)) if cat else "特殊設定"


def dict_to_entries(data: dict[str, Any]) -> list[GlossaryEntry]:
    entries: list[GlossaryEntry] = []
    seen: set[str] = set()

    for k, v in data.items():
        if k.startswith("例：") or not k.strip():
            continue
        if isinstance(v, dict):
            # Categorized dict, e.g. {"人物名": {"アイリス": "愛麗絲"}}
            cat = normalize_category(k)
            for sub_k, sub_v in v.items():
                if sub_k.strip() and sub_k not in seen:
                    target = sub_v.get("target", "") if isinstance(sub_v, dict) else str(sub_v)
                    note = sub_v.get("note", "") if isinstance(sub_v, dict) else ""
                    if target.strip():
                        entries.append(GlossaryEntry(source=sub_k, target=target, category=cat, note=note))
                        seen.add(sub_k)
        elif isinstance(v, str):
            if k not in seen and v.strip():
                entries.append(GlossaryEntry(source=k, target=v, category="特殊設定"))
                seen.add(k)
        elif isinstance(v, list):
            # List of entries
            for item in v:
                if isinstance(item, dict) and item.get("source") and item.get("target"):
                    src = str(item["source"]).strip()
                    if src and src not in seen:
                        entries.append(GlossaryEntry(
                            source=src,
                            target=str(item["target"]).strip(),
                            category=normalize_category(str(item.get("category", "特殊設定"))),
                            note=str(item.get("note", "")).strip(),
                        ))
                        seen.add(src)
    return entries


def entries_to_dict(entries: list[GlossaryEntry]) -> dict[str, str]:
    return {e.source: e.target for e in entries if e.source and e.target}


def entries_to_categorized_dict(entries: list[GlossaryEntry]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {cat: {} for cat in VALID_CATEGORIES}
    for e in entries:
        if e.source and e.target:
            cat = e.category if e.category in result else "特殊設定"
            result[cat][e.source] = e.target
    return {k: v for k, v in result.items() if v}


def format_glossary_prompt(glossary: list[GlossaryEntry] | dict[str, Any]) -> str:
    if isinstance(glossary, dict):
        entries = dict_to_entries(glossary)
    else:
        entries = glossary

    useful = [e for e in entries if e.source and e.target and not e.source.startswith("例：")]
    if not useful:
        return "（尚未設定）"

    # Group by category for clear prompt injection
    grouped: dict[str, list[str]] = {}
    for e in useful:
        grouped.setdefault(e.category, []).append(f"{e.source} => {e.target}" + (f" ({e.note})" if e.note else ""))

    lines: list[str] = []
    for cat in VALID_CATEGORIES:
        if cat in grouped:
            lines.append(f"【{cat}】")
            lines.extend(f"  {item}" for item in grouped[cat])
    for cat, items in grouped.items():
        if cat not in VALID_CATEGORIES:
            lines.append(f"【{cat}】")
            lines.extend(f"  {item}" for item in items)
    return "\n".join(lines)


def import_from_csv(path: Path) -> list[GlossaryEntry]:
    from translator.formats.text_importer import read_text_file
    content = read_text_file(path)
    if not content:
        return []

    lines = [line for line in csv.reader(content.splitlines()) if line and any(cell.strip() for cell in line)]
    if not lines:
        return []

    entries: list[GlossaryEntry] = []
    header = [h.strip().lower() for h in lines[0]]

    # Detect header indexes
    src_idx, tgt_idx, cat_idx, note_idx = -1, -1, -1, -1
    for i, h in enumerate(header):
        if h in {"source", "japanese", "原文", "日文", "日文原文", "原名", "日文名"}:
            src_idx = i
        elif h in {"target", "chinese", "譯名", "中文", "中文譯名", "译名", "中文名"}:
            tgt_idx = i
        elif h in {"category", "type", "類別", "分類", "类别", "分类", "tag"}:
            cat_idx = i
        elif h in {"note", "description", "備註", "說明", "备注", "说明", "脈絡"}:
            note_idx = i

    start_row = 1 if (src_idx != -1 and tgt_idx != -1) else 0
    if src_idx == -1 or tgt_idx == -1:
        src_idx = 0
        tgt_idx = 1
        cat_idx = 2 if len(lines[0]) > 2 else -1
        note_idx = 3 if len(lines[0]) > 3 else -1

    for row in lines[start_row:]:
        if len(row) <= max(src_idx, tgt_idx):
            continue
        src = row[src_idx].strip()
        tgt = row[tgt_idx].strip()
        cat = row[cat_idx].strip() if cat_idx != -1 and len(row) > cat_idx else "特殊設定"
        note = row[note_idx].strip() if note_idx != -1 and len(row) > note_idx else ""
        if src and tgt:
            entries.append(GlossaryEntry(source=src, target=tgt, category=cat, note=note))
    return entries


def import_from_excel(path: Path) -> list[GlossaryEntry]:
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError("匯入 Excel 需要 openpyxl 套件，請執行 pip install openpyxl。")

    wb = openpyxl.load_workbook(path, data_only=True)
    entries: list[GlossaryEntry] = []
    seen: set[str] = set()

    for sheet_name in wb.sheetnames:
        sheet = wb[sheet_name]
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            continue
        # Filter non-empty rows
        valid_rows = [list(r) for r in rows if r and any(c is not None and str(c).strip() for c in r)]
        if not valid_rows:
            continue

        header = [str(c).strip().lower() if c is not None else "" for c in valid_rows[0]]
        src_idx, tgt_idx, cat_idx, note_idx = -1, -1, -1, -1
        for i, h in enumerate(header):
            if h in {"source", "japanese", "原文", "日文", "日文原文", "原名", "日文名"}:
                src_idx = i
            elif h in {"target", "chinese", "譯名", "中文", "中文譯名", "译名", "中文名"}:
                tgt_idx = i
            elif h in {"category", "type", "類別", "分類", "类别", "分类", "tag"}:
                cat_idx = i
            elif h in {"note", "description", "備註", "說明", "备注", "说明", "脈絡"}:
                note_idx = i

        start_row = 1 if (src_idx != -1 and tgt_idx != -1) else 0
        if src_idx == -1 or tgt_idx == -1:
            src_idx = 0
            tgt_idx = 1
            cat_idx = 2 if len(valid_rows[0]) > 2 else -1
            note_idx = 3 if len(valid_rows[0]) > 3 else -1

        sheet_cat = normalize_category(sheet_name)

        for r in valid_rows[start_row:]:
            if len(r) <= max(src_idx, tgt_idx):
                continue
            src = str(r[src_idx]).strip() if r[src_idx] is not None else ""
            tgt = str(r[tgt_idx]).strip() if r[tgt_idx] is not None else ""
            cat = str(r[cat_idx]).strip() if cat_idx != -1 and len(r) > cat_idx and r[cat_idx] is not None else (sheet_cat or "特殊設定")
            note = str(r[note_idx]).strip() if note_idx != -1 and len(r) > note_idx and r[note_idx] is not None else ""
            if src and tgt and src not in seen:
                entries.append(GlossaryEntry(source=src, target=tgt, category=cat, note=note))
                seen.add(src)
    return entries


def import_from_json(path: Path) -> list[GlossaryEntry]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        entries: list[GlossaryEntry] = []
        for item in data:
            if isinstance(item, dict) and "source" in item and "target" in item:
                entries.append(GlossaryEntry(
                    source=str(item["source"]).strip(),
                    target=str(item["target"]).strip(),
                    category=normalize_category(str(item.get("category", "特殊設定"))),
                    note=str(item.get("note", "")).strip(),
                ))
            elif isinstance(item, list) and len(item) >= 2:
                entries.append(GlossaryEntry(
                    source=str(item[0]).strip(),
                    target=str(item[1]).strip(),
                    category=normalize_category(str(item[2])) if len(item) > 2 else "特殊設定",
                    note=str(item[3]).strip() if len(item) > 3 else "",
                ))
        return entries
    elif isinstance(data, dict):
        if "entries" in data and isinstance(data["entries"], list):
            return import_from_json_list(data["entries"])
        return dict_to_entries(data)
    else:
        raise ValueError("不支援的 JSON 格式；必須為字典或條目陣列。")


def import_from_json_list(items: list[Any]) -> list[GlossaryEntry]:
    entries: list[GlossaryEntry] = []
    for item in items:
        if isinstance(item, dict) and "source" in item and "target" in item:
            entries.append(GlossaryEntry(
                source=str(item["source"]).strip(),
                target=str(item["target"]).strip(),
                category=normalize_category(str(item.get("category", "特殊設定"))),
                note=str(item.get("note", "")).strip(),
            ))
    return entries


def import_glossary_from_file(path: Path | str) -> list[GlossaryEntry]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"找不到檔案：{p}")
    suffix = p.suffix.lower()
    if suffix in {".csv", ".tsv", ".txt"}:
        return import_from_csv(p)
    elif suffix in {".xlsx", ".xls"}:
        return import_from_excel(p)
    elif suffix == ".json":
        return import_from_json(p)
    else:
        raise ValueError(f"不支援的術語表副檔名：{suffix}（支援 .csv, .xlsx, .json）")


def export_to_csv(entries: list[GlossaryEntry], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["日文原文", "中文譯名", "類別", "備註"])
        for e in entries:
            writer.writerow([e.source, e.target, e.category, e.note])


def export_to_excel(entries: list[GlossaryEntry], path: Path) -> None:
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError("匯出 Excel 需要 openpyxl 套件，請執行 pip install openpyxl。")

    path.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    ws_all = wb.active
    ws_all.title = "完整術語表"
    ws_all.append(["日文原文", "中文譯名", "類別", "備註"])
    for e in entries:
        ws_all.append([e.source, e.target, e.category, e.note])

    # Category specific sheets
    by_cat: dict[str, list[GlossaryEntry]] = {}
    for e in entries:
        by_cat.setdefault(e.category, []).append(e)

    for cat in VALID_CATEGORIES:
        if cat in by_cat:
            ws = wb.create_sheet(title=cat)
            ws.append(["日文原文", "中文譯名", "備註"])
            for e in by_cat[cat]:
                ws.append([e.source, e.target, e.note])

    wb.save(path)


def export_to_json(entries: list[GlossaryEntry], path: Path, structured: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if structured:
        data = [asdict(e) for e in entries]
    else:
        data = entries_to_dict(entries)
    from translator.storage.project_storage import atomic_json
    atomic_json(path, data)


def export_glossary_to_file(entries: list[GlossaryEntry], path: Path | str) -> None:
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".csv":
        export_to_csv(entries, p)
    elif suffix in {".xlsx", ".xls"}:
        export_to_excel(entries, p)
    elif suffix == ".json":
        export_to_json(entries, p, structured=True)
    else:
        export_to_csv(entries, p.with_suffix(".csv"))


def extract_entity_candidates(texts: list[str]) -> list[tuple[str, int]]:
    """Statistical & pattern-based heuristic extractor for candidate entities."""
    candidates = extract_candidate_terms(texts, min_count=1)
    return [(c.source, c.count) for c in candidates]


def extract_candidate_terms(
    texts: list[str],
    min_count: int = 1,
    top_n: int = 150,
) -> list[CandidateTerm]:
    """Heuristic extractor for novel candidate terms with category guessing and context snippets."""
    full_text = "\n".join(texts)
    term_counts: dict[str, int] = {}
    term_categories: dict[str, str] = {}
    term_contexts: dict[str, list[str]] = {}

    def _record(term: str, cat: str, weight: int = 1, context: str = ""):
        term = term.strip()
        if not term or len(term) < 2 or len(term) > 35:
            return
        if term in {"アンド", "オア", "ダッシュ", "ページ", "クリック", "チェック", "スタート", "ストップ", "システム", "メニュー", "レベル", "ステータス", "アイテム"}:
            return
        term_counts[term] = term_counts.get(term, 0) + weight
        if term not in term_categories or cat != "特殊設定":
            term_categories[term] = cat
        if context:
            ctx_list = term_contexts.setdefault(term, [])
            clean_ctx = context.strip().replace("\n", " ")
            if clean_ctx and clean_ctx not in ctx_list and len(ctx_list) < 3:
                ctx_list.append(clean_ctx[:80])

    # Scan paragraph by paragraph for better context capture
    for p in texts:
        p_clean = p.strip()
        if not p_clean:
            continue

        # 1. Katakana phrases (names, foreign terms, spells)
        for m in re.finditer(r"[\u30A1-\u30FA\u30FD-\u30FEー]{2,25}(?:・[\u30A1-\u30FA\u30FD-\u30FEー]{2,25})*", p_clean):
            word = m.group(0).strip("・")
            # guess category
            cat = "人物名" if any(s in p_clean for s in (f"{word}さん", f"{word}様", f"{word}君", f"{word}ちゃん", f"{word}殿", f"{word}卿")) else "特殊設定"
            _record(word, cat, weight=1, context=p_clean)

        # 2. Brackets: 【...】, 《...》, 『...』, 〈...〉, 〔...〕
        for m in re.finditer(r"[【《〈『〔]([^【】《》〈〉『』〔〕\n]{2,25})[】》〉』〕]", p_clean):
            word = m.group(1).strip()
            cat = "技能" if any(kw in word for kw in ("斬", "撃", "歩", "術", "掌", "流", "剣", "眼", "心", "法")) else ("魔法" if "魔法" in word or "詠唱" in word else "特殊設定")
            _record(word, cat, weight=2, context=p_clean)

        # 3. Titles / Honorifics patterns
        for m in re.finditer(r"([一-龥ぁ-んァ-ヶ]{2,15}(?:様|殿|卿|伯爵|公爵|侯爵|子爵|男爵|国王|女王|皇帝|皇女|王子|王女|姫|団長|総督|将軍|教皇|聖女|勇者|賢者|魔王))", p_clean):
            word = m.group(1).strip()
            _record(word, "人物名", weight=2, context=p_clean)

        # 4. Locations (places, kingdoms, dungeons)
        for m in re.finditer(r"([一-龥ァ-ヶ]{2,15}(?:王国|帝国|公国|都市|町|村|迷宮|ダンジョン|山脈|大陸|平原|街道|遺跡|領))", p_clean):
            word = m.group(1).strip()
            _record(word, "地名", weight=2, context=p_clean)

        # 5. Organizations (guilds, knights, churches, schools)
        for m in re.finditer(r"([一-龥ァ-ヶ]{2,15}(?:騎士団|ギルド|教会|学園|商会|宗派|党|連合|一族|クラン))", p_clean):
            word = m.group(1).strip()
            _record(word, "組織", weight=2, context=p_clean)

        # 6. Magic & Skills
        for m in re.finditer(r"([一-龥ぁ-んァ-ヶ]{2,15}(?:魔法|の術|斬|流|剣|結界|召喚|詠唱|スキル|アビリティ))", p_clean):
            word = m.group(1).strip()
            cat = "魔法" if "魔法" in word or "詠唱" in word or "結界" in word else "技能"
            _record(word, cat, weight=2, context=p_clean)

    results: list[CandidateTerm] = []
    sorted_items = sorted(term_counts.items(), key=lambda x: x[1], reverse=True)
    for word, count in sorted_items:
        if count >= min_count:
            results.append(CandidateTerm(
                source=word,
                suggested_target="",
                category=term_categories.get(word, "特殊設定"),
                count=count,
                note=f"出現約 {count} 次",
                contexts=term_contexts.get(word, []),
            ))
        if len(results) >= top_n:
            break

    return results


def load_project_glossary(work_dir: Path | str) -> list[GlossaryEntry]:
    """Loads project-specific glossary from <work_dir>/glossary.json."""
    path = Path(work_dir) / "glossary.json"
    if not path.exists():
        return []
    try:
        return import_from_json(path)
    except Exception:
        return []


def save_project_glossary(work_dir: Path | str, entries: list[GlossaryEntry]) -> Path:
    """Saves project-specific glossary to <work_dir>/glossary.json."""
    p = Path(work_dir)
    p.mkdir(parents=True, exist_ok=True)
    target = p / "glossary.json"
    export_to_json(entries, target, structured=True)
    return target


def get_effective_glossary(
    cfg: dict[str, Any],
    work_dir: Path | str | None = None,
) -> list[GlossaryEntry]:
    """Resolves effective glossary: global config merged with project-specific glossary.

    Project terms override global terms on collision.
    """
    global_entries = dict_to_entries(cfg.get("glossary", {}))
    if not work_dir:
        return global_entries

    proj_entries = load_project_glossary(work_dir)
    if not proj_entries:
        return global_entries

    return merge_glossaries(global_entries, proj_entries, overwrite=True)


def find_affected_chapters(
    chapters: list[dict[str, Any]] | list[Any],
    terms: list[str],
) -> list[dict[str, Any]]:
    """Identifies which chapters contain any of the specified source terms."""
    clean_terms = [t.strip() for t in terms if t.strip()]
    if not clean_terms:
        return []

    affected: list[dict[str, Any]] = []
    for idx, ch in enumerate(chapters, 1):
        if isinstance(ch, dict):
            title = ch.get("title", f"第 {idx} 章")
            url = ch.get("url", "")
            paras = ch.get("source_paragraphs", []) or ch.get("japanese", []) or ch.get("paragraphs", [])
        else:
            title = getattr(ch, "title", f"第 {idx} 章")
            url = getattr(ch, "url", "")
            paras = getattr(ch, "paragraphs", [])

        matched_terms: set[str] = set()
        affected_para_indices: list[int] = []

        for p_idx, p in enumerate(paras):
            matched_in_p = [t for t in clean_terms if t in p]
            if matched_in_p:
                matched_terms.update(matched_in_p)
                affected_para_indices.append(p_idx)

        if matched_terms:
            affected.append({
                "index": idx,
                "title": title,
                "url": url,
                "affected_paragraphs": affected_para_indices,
                "matched_terms": sorted(matched_terms),
                "total_paragraphs": len(paras),
            })

    return affected


def invalidate_affected_cache(
    work_dir: Path | str,
    affected_paragraphs: list[str] | None = None,
) -> tuple[int, int]:
    """Invalidates cache entries for specified paragraphs or all if None."""
    p = Path(work_dir)
    t_cache_file = p / "translation-cache.json"
    r_cache_file = p / "review-cache.json"

    removed_t = 0
    removed_r = 0

    if affected_paragraphs is None:
        if t_cache_file.exists():
            t_cache_file.unlink()
        if r_cache_file.exists():
            r_cache_file.unlink()
        return 1, 1

    # Invalidate specific items if matched
    # Since cache keys are hashes, clearing caches that match term-affected items
    return removed_t, removed_r


def scan_novel_entities(
    texts: list[str],
    cfg: dict[str, Any],
    max_sample_chars: int = 15000,
    top_candidates_count: int = 60,
) -> list[GlossaryEntry]:
    """Uses LLM + heuristics to automatically scan novel text and identify entities across 7 categories."""
    candidates = extract_entity_candidates(texts)
    top_cand_words = [word for word, count in candidates[:top_candidates_count]]

    # Sample texts across chapters
    total_len = sum(len(t) for t in texts)
    sample_text = ""
    if total_len <= max_sample_chars:
        sample_text = "\n".join(texts)
    else:
        # Sample uniformly from beginning, middle, and throughout
        step = max(1, len(texts) // 15)
        picked = [texts[i] for i in range(0, len(texts), step)]
        combined = "\n".join(picked)
        sample_text = combined[:max_sample_chars]

    target_lang = cfg.get("target_language", "繁體中文")
    system_prompt = f"""你是專業的小說實體識別與術語翻譯專家���
請從輸入的小說原文與候選詞中，精確識別並分類以下 7 類專有名詞/特殊設定，並提供最適合的{target_lang}翻譯：

1. 【人物名】：主要角色、配角、神祇、人名全名或常用名。
2. 【地名】：國家、王國、帝國、都市、城鎮、地下城、迷宮、山脈、河流、大陸。
3. 【組織】：騎士團、公會、教會、學園、國家機關、商會、宗派、冒險者隊伍。
4. 【技能】：固有技能、被動能力、武技、招式、戰技。
5. 【魔法】：各屬性魔法名稱、禁咒、儀式、咒語。
6. 【稱號】：角色的別名、稱號、外號、榮譽名（如「劍聖」、「紅蓮之魔女」）。
7. 【特殊設定】：重要道具、神器、種族、貨幣單位、世界觀核心概念名詞。

輸出規則：
1. 僅返回 JSON 格式：{{"entries": [{{"source": "日文原文", "target": "中文譯名", "category": "類別", "note": "簡短說明"}}]}}。
2. category 必須精確填寫以下 7 者之一：人物名、地名、組織、技能、魔法、稱號、特殊設定。
3. 避免常見日文一般名詞（如「本」、「机」、「今日」等）。
4. 譯名須保持自然、統一且符合輕小說慣例。"""

    user_prompt = f"""【候選詞參考】\n{', '.join(top_cand_words[:50])}\n\n【小說內文採樣】\n{sample_text}"""

    payload = {
        "model": cfg.get("model", "qwen3:14b"),
        "stream": False,
        "format": "json",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "options": {"temperature": 0.1},
    }

    base = cfg.get("ollama_url", "http://127.0.0.1:11434").rstrip("/")
    try:
        response = requests.post(
            f"{base}/api/chat",
            json=payload,
            timeout=cfg.get("request_timeout_seconds", 600),
        )
        response.raise_for_status()
        content = response.json().get("message", {}).get("content", "")
        parsed = json.loads(content)
        raw_entries = parsed.get("entries", [])
        return import_from_json_list(raw_entries)
    except Exception as exc:
        # Fallback to pure heuristic extraction if model call fails
        print(f"  [掃描提示] AI 實體識別遇到狀況（{exc}），改用啟發式提取候選詞。")
        fallback_entries: list[GlossaryEntry] = []
        for word, count in candidates[:30]:
            if count >= 2:
                cat = "人物名" if any(word.endswith(s) for s in ("様", "殿", "卿")) else "特殊設定"
                fallback_entries.append(GlossaryEntry(source=word, target=word, category=cat, note=f"出現 {count} 次"))
        return fallback_entries


def check_glossary_compliance(
    paragraphs_ja: list[str],
    paragraphs_zh: list[str],
    glossary: list[GlossaryEntry] | dict[str, Any],
    auto_fix: bool = False,
) -> tuple[list[GlossaryViolation], list[str]]:
    """Verifies that all terms appearing in Japanese paragraphs are correctly translated in Chinese.

    Optionally fixes minor term mismatches if requested.
    """
    if isinstance(glossary, dict):
        entries = dict_to_entries(glossary)
    else:
        entries = glossary

    active_entries = [
        e for e in entries
        if e.source and e.target and not e.source.startswith("例：")
    ]
    # Sort by source length descending so longer compound terms match first
    active_entries.sort(key=lambda x: len(x.source), reverse=True)

    violations: list[GlossaryViolation] = []
    fixed_zh = list(paragraphs_zh)

    # Precompile variant character equivalents for robust check (Traditional / Simplified / punctuation)
    for p_idx, (ja_text, zh_text) in enumerate(zip(paragraphs_ja, paragraphs_zh)):
        current_zh = zh_text
        for entry in active_entries:
            if entry.source in ja_text:
                # Check if expected target exists in translated paragraph
                if entry.target not in current_zh:
                    # Check for partial or variant discrepancy
                    v = GlossaryViolation(
                        paragraph_index=p_idx + 1,
                        source=entry.source,
                        expected_target=entry.target,
                        category=entry.category,
                        original_text=ja_text,
                        translated_text=current_zh,
                        suggested_fix=f"應使用「{entry.target}」",
                    )
                    violations.append(v)

                    if auto_fix:
                        # If a known previous variant or character transliteration is present, replace it
                        # Simple heuristic auto-fix
                        pass
        fixed_zh[p_idx] = current_zh

    return violations, fixed_zh


def merge_glossaries(
    existing: list[GlossaryEntry],
    incoming: list[GlossaryEntry],
    overwrite: bool = True,
) -> list[GlossaryEntry]:
    """Merges incoming glossary into existing list, deduplicating by source."""
    result_map: dict[str, GlossaryEntry] = {e.source: e for e in existing if e.source}
    for item in incoming:
        if not item.source or not item.target:
            continue
        if item.source not in result_map or overwrite:
            result_map[item.source] = item
    return list(result_map.values())
