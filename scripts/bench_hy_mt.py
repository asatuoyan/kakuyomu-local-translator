"""Local synthetic translation benchmark; never reads or uploads novel files."""
import json
import time
import argparse
from pathlib import Path
import sys

# Keep direct execution from scripts/ working, alongside python -m scripts.bench_hy_mt.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import translator.engine as main


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", nargs="+", type=int, default=[5, 10], choices=range(1, 13))
    args = parser.parse_args()
    cfg = main.load_config()
    cfg.update(model="hy-mt2-30b:latest", target_language="简体中文",
               request_timeout_seconds=90, glossary={})
    places = ["図書館", "市場", "橋", "学校", "公園", "駅", "港", "劇場", "森", "広場", "工房", "城"]
    paragraphs = [
        f"第{i + 1}日の朝、私は{place}へ向かった。雨が止んだばかりで、道には小さな水たまりが残っていた。"
        f"友人は入り口で待っていて、今日の予定を書いた紙を渡してくれた。私たちはまず周囲の様子を確かめ、"
        f"必要な道具を集めることにした。夕方までに仕事を終えれば、家族と一緒に食事ができるはずだった。"
        for i, place in enumerate(places)]
    original_chat = main.ollama_chat_content
    responses = []
    def capture(payload, request_cfg):
        content = original_chat(payload, request_cfg)
        responses.append({"chars": len(content), "prefix": content[:100], "suffix": content[-80:]})
        return content
    main.ollama_chat_content = capture
    for count in args.sizes:
        metrics_list = []
        responses.clear()
        cfg["_translation_metrics"] = metrics_list.append
        start = time.monotonic()
        result = main.translate_chunk(paragraphs[:count], cfg)
        seconds = time.monotonic() - start
        metrics = {key: sum(item.get(key, 0) for item in metrics_list)
                   for key in ("load_duration", "prompt_eval_count", "prompt_eval_duration",
                               "eval_count", "eval_duration", "total_duration")}
        source_chars = sum(map(len, paragraphs[:count]))
        generation_seconds = metrics.get("eval_duration", 0) / 1e9
        print(json.dumps({"source_chars": source_chars, "paragraphs": count,
                          "returned_paragraphs": len(result), "seconds": round(seconds, 2),
                          "source_chars_per_second": round(source_chars / seconds, 2),
                          "generation_tokens_per_second": round(metrics.get("eval_count", 0) / generation_seconds, 2)
                          if generation_seconds else None, "requests": len(metrics_list),
                          "metrics": metrics, "response_samples": list(responses)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    run()
