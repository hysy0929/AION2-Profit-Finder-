#!/usr/bin/env python3
"""AION2 利益ファインダー：データ収集スクリプト（標準ライブラリのみ）

aion2.exchange（有志の非公式相場サイト）の公開APIから、指定サーバーの
  ・最安出品価格 / 出品数 / 24時間の販売数・販売平均価格（全アイテム）
  ・利益が出そうな候補アイテムの7日分の価格履歴
を取得し、表示ページ（site/index.html）が読む静的JSONに書き出す。
利益の判定（税・手数料・しきい値）は表示ページ側で行うので、設定はスマホから変えられる。

使い方:
  python collector/collect.py --out _site/data
  python collector/collect.py --list-servers        # サーバー名とIDを表示

出力:
  <out>/index.json          生成時刻、対象サーバー、全サーバー一覧
  <out>/s/<server_id>.json  そのサーバーの全アイテム（履歴があるものは7日中央値とミニチャート付き）
"""
from __future__ import annotations

import argparse
import os
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# フォルダ分けしてもしなくても動くように：config.json がある場所をリポジトリの最上位とみなす
ROOT = _HERE if (_HERE / "config.json").exists() else _HERE.parent
UA = "aion2-profit-finder/1.0 (personal dashboard; hourly)"
PAGE = 200  # /api/items の1ページ件数


class Api:
    """相手は個人運営のサイトなので、間隔を空けて控えめに取得する。"""

    def __init__(self, base: str, delay: float):
        self.base = base.rstrip("/")
        self.delay = delay
        self._last = 0.0
        self.calls = 0

    def get(self, path: str, params: dict | None = None, retries: int = 4):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        err: Exception | None = None
        for attempt in range(retries):
            wait = self.delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.calls += 1
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                err = e
                ra = e.headers.get("Retry-After") if e.headers else None
                busy = e.code in (429, 502, 503, 504)
                time.sleep(min(int(ra) if ra and ra.isdigit() else (5 * 2 ** attempt if busy else 2 ** attempt), 60))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
                err = e
                time.sleep(min(2 ** attempt, 10))
        raise RuntimeError(f"{url}: {err}")


def num(v):
    try:
        f = float(v)
        return f if f == f else None  # NaN除外
    except (TypeError, ValueError):
        return None


def parse_ts(ts) -> datetime | None:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


# ── 取得 ─────────────────────────────────────────────────────────────────


def fetch_servers(api: Api) -> list[dict]:
    j = api.get("/api/servers") or {}
    out = []
    for s in j.get("servers") or []:
        out.append({k: s.get(k) for k in ("server_id", "name", "region", "region_name", "faction")})
    return out


def pick_servers(all_servers: list[dict], wanted: list) -> list[dict]:
    picked = []
    for w in wanted:
        for s in all_servers:
            if str(s.get("server_id")) == str(w) or (s.get("name") or "").strip().lower() == str(w).strip().lower():
                if s not in picked:
                    picked.append(s)
    return picked


def fetch_items(api: Api, sid) -> list[dict]:
    rows, off = [], 0
    while True:
        try:
            j = api.get("/api/items", {"server": sid, "limit": PAGE, "offset": off, "window": "24h"}) or {}
        except RuntimeError as e:
            # 2ページ目以降で失敗したら（最終ページの先で503を返すことがある）、取れた分で続ける
            if not rows:
                raise
            print(f"  {off}件目以降は取得できず（{e}）。取れた {len(rows)} 件で続行", file=sys.stderr)
            return rows
        items = j.get("items") or []
        rows += items
        off += PAGE
        if len(items) < PAGE or (j.get("total") and len(rows) >= j["total"]) or off > 20000:
            return rows


def norm_item(r: dict) -> dict | None:
    key = r.get("item_key", r.get("id"))
    if key is None:
        return None
    cat = r.get("category") or {}
    return {
        "k": str(key),
        "n": r.get("name") or str(key),
        "c": cat.get("name") if isinstance(cat, dict) else None,
        "sec": cat.get("section_name") if isinstance(cat, dict) else None,
        "p": num(r.get("price")),            # 最安出品単価
        "l": num(r.get("listings")),         # 出品数
        "s": num(r.get("units_sold")),       # 24時間の販売数（推定）
        "a": num(r.get("sold_avg_price")),   # 24時間の販売平均単価
        "t": r.get("captured_at"),
        "ic": icon_of(r),
        "chA": num(r.get("change_pct")),
        "chB": num(r.get("change_basis_seconds")),
        "g": r.get("grade") or r.get("rarity") or r.get("quality"),
    }


def icon_of(r: dict):
    for k in ("icon_url", "icon", "image_url", "image", "img", "iconUrl"):
        v = r.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def dump_debug(api: Api, sid, out: Path) -> None:
    """APIの実際の返り値の一部を debug.json に残す（項目名の確認用。公開されるのは相場データのみ）。"""
    dbg: dict = {}
    for label, params in (("items_window24h", {"server": sid, "limit": 3, "offset": 0, "window": "24h"}),
                          ("items_plain", {"server": sid, "limit": 3}),
                          ("items_sort_sold", {"server": sid, "limit": 3, "sort": "units_sold"})):
        try:
            dbg[label] = api.get("/api/items", params, retries=1)
        except Exception as e:
            dbg[label] = f"error: {e}"
    try:
        first = (dbg.get("items_plain") or {}).get("items", [{}])[0]
        key = first.get("item_key", first.get("id"))
        if key is not None:
            for label, path, params in (("item_detail", f"/api/items/{key}", {"server": sid}),
                                        ("prices", f"/api/items/{key}/prices", {"server": sid}),
                                        ("sales", f"/api/items/{key}/sales", {"server": sid})):
                try:
                    j = api.get(path, params, retries=1)
                    if isinstance(j, dict) and isinstance(j.get("points"), list):
                        j = {**j, "points": j["points"][:3], "_points_total": len(j["points"])}
                    dbg[label] = j
                except Exception as e:
                    dbg[label] = f"error: {e}"
    except Exception as e:
        dbg["detail_error"] = str(e)
    (out / "debug.json").write_text(json.dumps(dbg, ensure_ascii=False, indent=1, default=str)[:60000], encoding="utf-8")


def fetch_history(api: Api, sid, key: str, days: int) -> list[tuple[int, float | None, float | None]]:
    """aion2.exchange の1時間ごとの履歴（最安値と出品数）。戻り値は (時刻番号, 最安値, 出品数)。"""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    j = api.get(f"/api/items/{urllib.parse.quote(key)}/prices", {"server": sid, "bucket": "1h", "from": since}) or {}
    pts = []
    for pt in j.get("points") or []:
        t = parse_ts(pt.get("ts"))
        if not t:
            continue
        p = num(pt.get("low", pt.get("price")))
        pts.append((int(t.timestamp() // 3600), p if p and p > 0 else None, num(pt.get("listings_min", pt.get("listings")))))
    return pts


# ── 自前の履歴（毎時の最安値と出品数を7日分ためる） ─────────────────────────
# aion2.exchange はこのサーバーの販売統計を持っていない（units_sold が常に空）。
# そこで出品数の推移を毎時記録し、「出品数が減った件数」を販売の目安にする（取り下げも含む）。


class History:
    def __init__(self, prev: dict | None, keep_hours: int):
        self.keep = keep_hours
        self.d: dict[str, dict[int, list]] = {}
        for k, rows in ((prev or {}).get("items") or {}).items():
            self.d[k] = {int(h): [p, l] for h, p, l in rows}

    def put(self, k: str, h: int, p, l, overwrite=True):
        cur = self.d.setdefault(k, {})
        if overwrite or h not in cur:
            cur[h] = [p, l]

    def count(self, k: str) -> int:
        return len(self.d.get(k) or {})

    def trim(self, now_h: int):
        lo = now_h - self.keep
        for k in list(self.d):
            self.d[k] = {h: v for h, v in self.d[k].items() if h > lo}
            if not self.d[k]:
                del self.d[k]

    def dump(self) -> dict:
        return {"items": {k: [[h, v[0], v[1]] for h, v in sorted(rows.items())] for k, rows in self.d.items()}}

    def features(self, k: str, now_h: int, days: int) -> dict:
        rows = self.d.get(k) or {}
        if not rows:
            return {}
        out: dict = {}
        lows = [v[0] for v in rows.values() if v[0]]
        if lows:
            out.update(m7=round(statistics.median(lows)), lo7=round(min(lows)), hi7=round(max(lows)), n7=len(lows))
        day = [rows[h] for h in sorted(rows) if h > now_h - 24]
        day_lows = [v[0] for v in day if v[0]]
        if day_lows:
            out["a"] = round(statistics.median(day_lows))  # 24時間の最安値の中央値
        ls = [v[1] for v in day if v[1] is not None]
        if len(ls) >= 2:
            out["s"] = int(sum(max(0, x - y) for x, y in zip(ls, ls[1:])))  # 出品数の減少合計
            out["sh"] = len(ls)  # 何時間分のデータで数えたか
        cur = rows.get(now_h, [None])[0] or rows.get(now_h - 1, [None])[0]
        for label, back in (("ch6", 6), ("ch24", 24)):
            past = [rows[h][0] for h in range(now_h - back - 2, now_h - back + 3) if h in rows and rows[h][0]]
            if cur and past:
                out[label] = round(cur / statistics.median(past) - 1, 4)
        spark = []
        for start in range(now_h - days * 24 + 1, now_h + 1, 12):
            v = [rows[h][0] for h in range(start, start + 12) if h in rows and rows[h][0]]
            spark.append(round(min(v)) if v else None)
        out["sp"] = spark
        return out


def load_prev_json(url: str | None):
    if not url:
        return None
    try:
        req = urllib.request.Request(url + ("&" if "?" in url else "?") + f"t={int(time.time())}", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except Exception as e:  # 初回は404
        print(f"前回データなし（{url}: {e}）")
        return None


def pages_base() -> str | None:
    """GitHub Actions 上なら公開ページのURL（末尾 /）。"""
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo or "/" not in repo:
        return None
    owner, name = repo.split("/", 1)
    if name.lower() == f"{owner.lower()}.github.io":
        return f"https://{owner.lower()}.github.io/"
    return f"https://{owner.lower()}.github.io/{name}/"


def recipe_keys() -> set[str]:
    path = ROOT / "recipes.json"
    if not path.exists():
        return set()
    try:
        recipes = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"recipes.json を読めません: {e}", file=sys.stderr)
        return set()
    keys = set()
    for r in recipes if isinstance(recipes, list) else []:
        for part in [r.get("output") or {}] + list(r.get("materials") or []):
            if part.get("key"):
                keys.add(str(part["key"]))
    return keys


# ── 日本語のアイテム名 ───────────────────────────────────────────────────
# aion2.exchange の名前は英語なので、NC公式サイトのアイテム情報APIから日本語名を取って
# names.json（アイテムID → 日本語名）に貯める。前回公開分を読み戻し、毎回少しずつ増やす。
# NCはURLの形をときどき変えるので、いくつかの候補を試して日本語が返ったものを使う。

NC_NAME_URLS = [
    ("https://aion2.plaync.com/ja-jp/api/gameconst/item", {"lang": "ja-JP", "region": "jp"}),
    ("https://aion2.plaync.com/ja-jp/api/gameconst/item", {"lang": "ja-JP", "region": "as"}),
    ("https://aion2.plaync.com/ja-jp/api/gameconst/item", {"lang": "ja-JP"}),
    ("https://aion2.plaync.com/api/gameconst/item", {"lang": "ja-JP", "region": "jp"}),
    ("https://aion2.plaync.com/api/gameconst/item", {"lang": "ja-JP"}),
]


def default_prev_names_url() -> str | None:
    """GitHub Actions 上なら、公開ページのURLを自動で組み立てる（https://owner.github.io/repo/data/names.json）。"""
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo or "/" not in repo:
        return None
    owner, name = repo.split("/", 1)
    if name.lower() == f"{owner.lower()}.github.io":
        return f"https://{owner.lower()}.github.io/data/names.json"
    return f"https://{owner.lower()}.github.io/{name}/data/names.json"


def is_japanese(s) -> bool:
    return isinstance(s, str) and any("぀" <= ch <= "ヿ" or "一" <= ch <= "鿿" for ch in s)


def load_prev_names(url: str | None) -> dict:
    if not url:
        return {}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
        return {str(k): v for k, v in d.items() if is_japanese(v)} if isinstance(d, dict) else {}
    except Exception as e:  # 初回は404になる
        print(f"前回の日本語名は読めませんでした（{e}）")
        return {}


def nc_name(url: str, params: dict, key: str):
    try:
        j = _get_abs(url, {**params, "id": key, "enchantLevel": 0})
    except Exception:
        return None
    if isinstance(j, dict):
        if isinstance(j.get("data"), dict):
            j = j["data"]
        n = j.get("name")
        if is_japanese(n):
            return n
    return None


def _get_abs(url: str, params: dict):
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": UA, "Accept": "application/json", "Accept-Language": "ja-JP"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def fill_names(keys: list[str], names: dict, limit: int) -> str:
    todo = [k for k in keys if k not in names]
    if not todo:
        return f"日本語名：{len(names)}件（追加なし）"
    probe = todo[:3]
    chosen = None
    for url, params in NC_NAME_URLS:
        if any(nc_name(url, params, k) for k in probe):
            chosen = (url, params)
            break
        time.sleep(0.3)
    if not chosen:
        return f"日本語名：NC公式から取得できませんでした（試したURL {len(NC_NAME_URLS)}種）。手元の {len(names)} 件を使用"
    added = 0
    for k in todo[:limit]:
        n = nc_name(chosen[0], chosen[1], k)
        if n:
            names[k] = n
            added += 1
        time.sleep(0.2)
    return f"日本語名：{added}件追加（計{len(names)}件、残り{max(0, len(todo) - limit)}件は次回以降）取得元 {chosen[0]} {chosen[1]}"


# ── メイン ───────────────────────────────────────────────────────────────


def build_server(api: Api, server: dict, cfg: dict, rkeys: set[str], out: Path, prev_base: str | None) -> dict:
    sid = server["server_id"]
    now = datetime.now(timezone.utc)
    now_h = int(now.timestamp() // 3600)
    days = cfg["history_days"]
    hist = History(load_prev_json(prev_base and f"{prev_base}data/s/{sid}_h.json"), days * 24)
    raw = fetch_items(api, sid)
    items = [x for x in (norm_item(r) for r in raw) if x]

    # 今回の観測を記録（出品なしは出品数0として記録）
    for it in items:
        t = parse_ts(it["t"])
        h = int(t.timestamp() // 3600) if t else now_h
        if it["p"] and h >= now_h - 2:
            hist.put(it["k"], h, it["p"], it["l"] or 0)
        else:
            hist.put(it["k"], now_h, None, 0)

    # 自前の記録が薄い品目は、aion2.exchange の履歴で埋める（出品の多い順、1回あたり上限あり）
    thin = [it for it in items if hist.count(it["k"]) < 24 and (it["l"] or 0) > 0]
    thin.sort(key=lambda it: (it["k"] not in rkeys, -(it["l"] or 0)))
    got = 0
    for it in thin[: cfg["history_budget_per_server"]]:
        try:
            for h, p, l in fetch_history(api, sid, it["k"], days):
                hist.put(it["k"], h, p, l, overwrite=False)
            got += 1
        except RuntimeError as e:
            print(f"履歴 {sid}/{it['k']}: {e}", file=sys.stderr)
    hist.trim(now_h)

    for it in items:
        api_s, api_a = it["s"], it["a"]
        f = hist.features(it["k"], now_h, days)
        it.update(f)
        if api_s is not None:  # 公式な販売統計があればそちらを優先
            it["s"], it["a"], it["est"] = api_s, api_a or it.get("a"), False
        else:
            it["est"] = True

    captured = max((it["t"] or "" for it in items), default="") or None
    doc = {"server": server, "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "captured_at": captured,
           "history_days": days, "items": items}
    (out / "s").mkdir(parents=True, exist_ok=True)
    (out / "s" / f"{sid}.json").write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (out / "s" / f"{sid}_h.json").write_text(json.dumps(hist.dump(), separators=(",", ":")), encoding="utf-8")
    # 詳細画面用：アイテムごとの1時間ごとの最安値と出品数
    idir = out / "s" / str(sid)
    idir.mkdir(parents=True, exist_ok=True)
    for k, rows in hist.d.items():
        (idir / f"{k}.json").write_text(json.dumps({"h": [[h, v[0], v[1]] for h, v in sorted(rows.items())]},
                                                   separators=(",", ":")), encoding="utf-8")
    remain = max(0, len(thin) - cfg["history_budget_per_server"])
    print(f"{server.get('name')}({sid}): {len(items)}品目、履歴の補完 {got}件（残り{remain}件は次回以降）")
    return {**server, "items": len(items), "captured_at": captured}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="_site/data")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--base", default="https://aion2.exchange")
    ap.add_argument("--servers", help="config.json の servers を上書き（カンマ区切りの名前かID）")
    ap.add_argument("--list-servers", action="store_true")
    ap.add_argument("--prev-names", default=os.environ.get("PREV_NAMES_URL") or default_prev_names_url(), help="前回公開した names.json のURL")
    ap.add_argument("--names-per-run", type=int, default=400)
    ap.add_argument("--prev-base", help="前回公開分のURL（末尾/）。省略時はGitHub Actionsから自動")
    ap.add_argument("--debug", action="store_true", help="APIの返り値サンプルを debug.json に出す")
    a = ap.parse_args()

    cfg = {"servers": [], "history_days": 7, "history_budget_per_server": 80, "candidates_flip": 40,
           "candidates_volume": 30, "tax_rate": 0.10, "fee_rate": 0.02, "request_delay_sec": 0.3}
    cfg.update({k: v for k, v in json.loads(Path(a.config).read_text(encoding="utf-8")).items() if not k.startswith("_")})
    if a.servers:
        cfg["servers"] = [x.strip() for x in a.servers.split(",") if x.strip()]

    api = Api(a.base, cfg["request_delay_sec"])
    all_servers = fetch_servers(api)
    if a.list_servers:
        for s in all_servers:
            print(f"{s['server_id']}\t{s.get('region')}\t{s.get('name')}\t{s.get('faction') or ''}")
        return 0

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    targets = pick_servers(all_servers, cfg["servers"])
    if cfg["servers"] and not targets:
        print(f"config.json の servers {cfg['servers']} に一致するサーバーがありません。--list-servers で確認してください。", file=sys.stderr)

    rkeys = recipe_keys()
    base = a.prev_base if a.prev_base is not None else pages_base()
    if a.debug and targets:
        try:
            dump_debug(api, targets[0]["server_id"], out)
        except Exception as e:
            print(f"debug: {e}", file=sys.stderr)
    done = []
    for s in targets:
        try:
            done.append(build_server(api, s, cfg, rkeys, out, base))
        except RuntimeError as e:
            print(f"{s.get('name')}: 取得失敗 {e}", file=sys.stderr)

    index = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": a.base,
        "defaults": {"tax_rate": cfg["tax_rate"], "fee_rate": cfg["fee_rate"]},
        "servers": done,
        "all_servers": all_servers,
    }
    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"APIリクエスト {api.calls} 回")

    # 日本語名（失敗しても相場データの公開は止めない）
    names = load_prev_names(a.prev_names)
    keys: list[str] = []
    for s in done:
        try:
            doc = json.loads((out / "s" / f"{s['server_id']}.json").read_text(encoding="utf-8"))
            keys += [it["k"] for it in doc["items"] if it["k"] not in keys]
        except (OSError, KeyError, json.JSONDecodeError):
            pass
    try:
        print(fill_names(keys, names, a.names_per_run))
    except Exception as e:
        print(f"日本語名の取得でエラー：{e}", file=sys.stderr)
    (out / "names.json").write_text(json.dumps(names, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    # 直訳辞書（リポジトリの names_ja.json）を公開側へコピーし、未訳の英語名一覧を書き出す
    ja = {}
    src = ROOT / "names_ja.json"
    if src.exists():
        try:
            ja = json.loads(src.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"names_ja.json を読めません: {e}", file=sys.stderr)
    (out / "names_ja.json").write_text(json.dumps(ja, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    en: dict[str, str] = {}
    for s in done:
        try:
            doc = json.loads((out / "s" / f"{s['server_id']}.json").read_text(encoding="utf-8"))
            for it in doc["items"]:
                if it["k"] not in ja:
                    en.setdefault(it["k"], it["n"])
        except (OSError, KeyError, json.JSONDecodeError):
            pass
    (out / "names_todo.txt").write_text("".join(f"{k}\t{v}\n" for k, v in sorted(en.items(), key=lambda x: x[1])), encoding="utf-8")
    print(f"直訳辞書 {len(ja)}件、未訳 {len(en)}件")
    return 0 if (done or not targets) else 1


if __name__ == "__main__":
    sys.exit(main())
