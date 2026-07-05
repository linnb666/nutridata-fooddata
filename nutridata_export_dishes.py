import argparse
import base64
import concurrent.futures
import csv
import datetime as dt
import html
import json
import os
import random
import re
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


BASE_URL = "https://nutridata.cn"
API_BASE = f"{BASE_URL}/api"
DISH_DATABASE_ID = 2
PUBLIC_KEY = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDMog6RvWfK7CY22mZ0gsj05cDHlw66XyRxtaqQN1SfTznBpa7kMDjicl8PdCHK76Xj+kPU/uKrg"
    "VCUFfhWoX13bzDCAvmkVN37Kw4PLIDkOgFe/Oklakphkm0/YE5TXu52hMdt0k6RgrW2QaxksYwcQJ2xDG31hUje22ASvsaXWwIDAQAB"
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

BASE_COLUMNS = ["菜肴ID", "食物名", "可食部克重"]
PREFERRED_NUTRIENTS = ["能量", "蛋白质", "脂肪", "碳水化合物"]
NUTRIENT_GROUPS = ("能量及宏量营养素", "维生素", "矿物质")
HTML_TAG_RE = re.compile(r"<[^>]+>")
WEIGHT_TEXT_RE = re.compile(r"每\s*([0-9]+(?:\.[0-9]+)?)\s*克\s*可食部分计")
WEIGHT_KEY_RE = re.compile(r"(weight|gram|amount|quantity|measure|edible|computed|result|克重|计量|可食)", re.IGNORECASE)


class NutriDataError(RuntimeError):
    pass


class NutriDataClient:
    def __init__(self, token: str = "", timeout: int = 30) -> None:
        self.token = token
        self.timeout = timeout
        self.aes_key = self._random_key()
        pem = f"-----BEGIN PUBLIC KEY-----\n{PUBLIC_KEY}\n-----END PUBLIC KEY-----\n"
        self.public_key = serialization.load_pem_public_key(pem.encode("ascii"), backend=default_backend())

    @staticmethod
    def _random_key() -> str:
        alphabet = string.ascii_letters + string.digits
        return "".join(random.choice(alphabet) for _ in range(16))

    def _aes_encrypt(self, text: str) -> str:
        data = text.encode("utf-8")
        pad_len = 16 - (len(data) % 16)
        data += bytes([pad_len]) * pad_len
        encryptor = Cipher(
            algorithms.AES(self.aes_key.encode("utf-8")),
            modes.ECB(),
            backend=default_backend(),
        ).encryptor()
        return base64.b64encode(encryptor.update(data) + encryptor.finalize()).decode("ascii")

    def _aes_decrypt(self, text: str) -> str:
        decryptor = Cipher(
            algorithms.AES(self.aes_key.encode("utf-8")),
            modes.ECB(),
            backend=default_backend(),
        ).decryptor()
        data = decryptor.update(base64.b64decode(text)) + decryptor.finalize()
        return data[: -data[-1]].decode("utf-8")

    def _encrypted_key(self) -> str:
        encrypted = self.public_key.encrypt(self.aes_key.encode("utf-8"), padding.PKCS1v15())
        return base64.b64encode(encrypted).decode("ascii")

    def _request(
        self,
        path: str,
        payload: Any,
        *,
        content_type: str = "application/json",
        retries: int = 3,
    ) -> Dict[str, Any]:
        last_error: Optional[BaseException] = None
        for attempt in range(1, retries + 1):
            try:
                if content_type == "application/x-www-form-urlencoded":
                    body = urllib.parse.urlencode(payload)
                    url = f"{API_BASE}{path}?param={urllib.parse.quote(self._aes_encrypt(body), safe='')}"
                    data = b""
                else:
                    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                    url = f"{API_BASE}{path}"
                    data = self._aes_encrypt(body).encode("utf-8")

                request = urllib.request.Request(url, data=data, method="POST")
                request.add_header("Content-Type", content_type)
                request.add_header("User-Agent", USER_AGENT)
                request.add_header("nutridata-random", self._encrypted_key())
                if self.token:
                    request.add_header("nutridata-token", self.token)

                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    encrypted_response = response.read().decode("utf-8")
                decoded = self._aes_decrypt(encrypted_response)
                result = json.loads(decoded)
                if result.get("code") not in (200, None):
                    raise NutriDataError(f"{path} failed: {result.get('msg')} (code={result.get('code')})")
                return result
            except (urllib.error.URLError, TimeoutError, NutriDataError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < retries:
                    time.sleep(1.5 * attempt)
                    continue
                raise NutriDataError(str(last_error)) from last_error
        raise NutriDataError(str(last_error))

    def login(self, username: str, password: str) -> str:
        response = self._request(
            "/nutri-oauth/user/login",
            {"username": username, "password": password},
            content_type="application/x-www-form-urlencoded",
        )
        token = (response.get("result") or {}).get("token")
        if not token:
            raise NutriDataError(f"登录失败：{response.get('msg') or response}")
        self.token = token
        return token

    def get_db_info(self, db_id: int = DISH_DATABASE_ID) -> Dict[str, Any]:
        return self._request("/nutri-service/dblist/selectDbInfo", {"id": db_id}).get("result") or {}

    def get_dish_count(self, count_path: str, db_id: int = DISH_DATABASE_ID) -> int:
        result = self._request(f"/nutri-service/{count_path}", {"id": db_id, "page": 1, "pageSize": 10}).get("result")
        return int(result or 0)

    def get_dish_list(self, list_path: str, page: int, page_size: int, db_id: int = DISH_DATABASE_ID) -> List[Dict[str, Any]]:
        result = self._request(
            f"/nutri-service/{list_path}",
            {"id": db_id, "page": page, "pageSize": page_size},
        ).get("result") or {}
        return result.get("list") or result.get("data") or []

    def get_dish_detail(self, detail_path: str, dish_id: int, db_id: int = DISH_DATABASE_ID) -> Dict[str, Any]:
        return self._request(f"/nutri-service/{detail_path}", {"id": dish_id, "aid": db_id}).get("result") or {}


def clean_label(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = HTML_TAG_RE.sub("", text)
    text = text.replace("\u2081", "1").replace("\u2082", "2").replace("\u2086", "6")
    text = re.sub(r"\s+", "", text)
    return text


def as_number(value: Any) -> Any:
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if text in {"-", "--", "未检测", "未检出", "Tr", "trace"}:
        return text
    try:
        number = float(text)
    except ValueError:
        return text
    return int(number) if number.is_integer() else number


def nutrient_column(item: Dict[str, Any]) -> str:
    desc = clean_label(item.get("desc") or item.get("name") or item.get("ename"))
    unit = clean_label(item.get("unit"))
    return f"{desc}({unit})" if unit else desc


def extract_edible_weight(*sources: Any) -> Any:
    for source in sources:
        major_weight = sum_major_notes(source)
        if major_weight != "":
            return major_weight
    for source in sources:
        match = find_weight_text(source)
        if match != "":
            return match
    for source in sources:
        match = find_weight_field(source)
        if match != "":
            return match
    return ""


def sum_major_notes(value: Any) -> Any:
    if not isinstance(value, dict):
        return ""
    major = value.get("major")
    if not isinstance(major, list):
        return ""
    total = 0.0
    found = False
    for item in major:
        if not isinstance(item, dict):
            continue
        note = item.get("note")
        if isinstance(note, (int, float)) and not isinstance(note, bool):
            total += float(note)
            found = True
        elif isinstance(note, str):
            try:
                total += float(note.strip())
                found = True
            except ValueError:
                pass
    if not found:
        return ""
    return int(total) if total.is_integer() else round(total, 4)


def find_weight_text(value: Any) -> Any:
    if isinstance(value, dict):
        for child in value.values():
            match = find_weight_text(child)
            if match != "":
                return match
    elif isinstance(value, list):
        for child in value:
            match = find_weight_text(child)
            if match != "":
                return match
    elif isinstance(value, str):
        text = html.unescape(HTML_TAG_RE.sub("", value))
        match = WEIGHT_TEXT_RE.search(text)
        if match:
            return as_number(match.group(1))
    return ""


def find_weight_field(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        if key and WEIGHT_KEY_RE.search(key):
            scalar = scalar_weight_value(value)
            if scalar != "":
                return scalar
        for child_key, child_value in value.items():
            match = find_weight_field(child_value, str(child_key))
            if match != "":
                return match
    elif isinstance(value, list):
        for child in value:
            match = find_weight_field(child, key)
            if match != "":
                return match
    elif key and WEIGHT_KEY_RE.search(key):
        return scalar_weight_value(value)
    return ""


def scalar_weight_value(value: Any) -> Any:
    if isinstance(value, dict):
        for key in ("result", "value", "weight", "num", "amount", "quantity"):
            if key in value:
                scalar = scalar_weight_value(value[key])
                if scalar != "":
                    return scalar
        return ""
    if isinstance(value, (list, tuple)):
        return ""
    if isinstance(value, str):
        text = html.unescape(HTML_TAG_RE.sub("", value)).strip()
        match = WEIGHT_TEXT_RE.search(text)
        if match:
            return as_number(match.group(1))
        gram_match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*(g|克)?", text, re.IGNORECASE)
        if gram_match:
            return as_number(gram_match.group(1))
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return as_number(value)
    return ""


def flatten_detail(detail: Dict[str, Any], list_item: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    list_item = list_item or {}
    dish_id = detail.get("id") or list_item.get("id")
    name = detail.get("name") or list_item.get("name")

    # 计算可食部克重：从 major 中累加 note（克数）
    edible_weight = ""
    major_list = detail.get("major")
    if isinstance(major_list, list) and major_list:
        total = 0.0
        for item in major_list:
            note = item.get("note")
            if note is not None:
                try:
                    total += float(note)
                except (ValueError, TypeError):
                    pass
        if total > 0:
            edible_weight = as_number(total)

    row: Dict[str, Any] = {
        "菜肴ID": dish_id,
        "食物名": clean_label(name),
        "可食部克重": edible_weight,
    }

    nutrition_map = detail.get("nutritionMap") or detail.get("nutriGroup") or {}
    for group in NUTRIENT_GROUPS:
        for item in nutrition_map.get(group) or []:
            row[nutrient_column(item)] = as_number(item.get("value"))
    return row


def load_progress(progress_path: Path) -> Dict[int, Dict[str, Any]]:
    rows: Dict[int, Dict[str, Any]] = {}
    if not progress_path.exists():
        return rows
    with progress_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            item = json.loads(line)
            dish_id = item.get("菜肴ID")
            if dish_id:
                rows[int(dish_id)] = item
    return rows


def append_progress(progress_path: Path, row: Dict[str, Any]) -> None:
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    with progress_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def collect_list_items(
    client: NutriDataClient,
    list_path: str,
    total: int,
    page_size: int,
    max_items: int = 0,
) -> Dict[int, Dict[str, Any]]:
    items: Dict[int, Dict[str, Any]] = {}
    target_total = min(total, max_items) if max_items else total
    page = 1
    while len(items) < target_total:
        page_items = client.get_dish_list(list_path, page, page_size)
        if not page_items:
            break
        for item in page_items:
            if item.get("id") is not None:
                items[int(item["id"])] = item
            if len(items) >= target_total:
                break
        print(f"列表页 {page}，累计 {len(items)}/{target_total}", flush=True)
        page += 1
    return items


def export_rows(rows: Sequence[Dict[str, Any]], output_path: Path) -> None:
    headers = build_headers(rows)
    if output_path.suffix.lower() == ".xlsx":
        write_xlsx(output_path, headers, rows)
    else:
        write_csv(output_path, headers, rows)


def build_headers(rows: Sequence[Dict[str, Any]]) -> List[str]:
    seen = set(BASE_COLUMNS)
    headers = list(BASE_COLUMNS)
    discovered: List[str] = []
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                discovered.append(key)

    def is_preferred(column: str) -> Tuple[int, int]:
        for idx, label in enumerate(PREFERRED_NUTRIENTS):
            if column.startswith(label):
                return (0, idx)
        return (1, len(PREFERRED_NUTRIENTS))

    preferred = [col for col in discovered if is_preferred(col)[0] == 0]
    preferred.sort(key=is_preferred)
    others = [col for col in discovered if col not in preferred]
    return headers + preferred + others


def write_csv(path: Path, headers: Sequence[str], rows: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def col_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def cell_xml(row_index: int, col_index: int, value: Any) -> str:
    ref = f"{col_name(col_index)}{row_index}"
    if value is None or value == "":
        return f'<c r="{ref}"/>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}" t="n"><v>{value}</v></c>'
    escaped = html.escape(str(value), quote=False)
    return f'<c r="{ref}" t="inlineStr"><is><t>{escaped}</t></is></c>'


def write_xlsx(path: Path, headers: Sequence[str], rows: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    max_col = len(headers)
    max_row = len(rows) + 1
    dimension = f"A1:{col_name(max_col)}{max_row}"

    sheet_parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
        f'<dimension ref="{dimension}"/>',
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
        'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>',
        '<sheetData>',
        '<row r="1">',
    ]
    sheet_parts.extend(cell_xml(1, idx, header) for idx, header in enumerate(headers, 1))
    sheet_parts.append("</row>")
    for row_index, row in enumerate(rows, 2):
        sheet_parts.append(f'<row r="{row_index}">')
        for col_index, header in enumerate(headers, 1):
            sheet_parts.append(cell_xml(row_index, col_index, row.get(header, "")))
        sheet_parts.append("</row>")
    sheet_parts.extend(["</sheetData>", f'<autoFilter ref="{dimension}"/>', "</worksheet>"])

    now = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
            '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
            "</Relationships>",
        )
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="菜肴营养数据" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            "</Relationships>",
        )
        archive.writestr(
            "xl/styles.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
            '<borders count="1"><border/></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
            "</styleSheet>",
        )
        archive.writestr("xl/worksheets/sheet1.xml", "".join(sheet_parts))
        archive.writestr(
            "docProps/core.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            "<dc:creator>nutridata_export_dishes.py</dc:creator>"
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
            f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>'
            "</cp:coreProperties>",
        )
        archive.writestr(
            "docProps/app.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
            'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
            "<Application>Python</Application></Properties>",
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="导出 NutriData 菜肴库营养数据到 CSV/Excel。")
    parser.add_argument("--username", default=os.getenv("NUTRIDATA_USERNAME", ""), help="nutridata.cn 用户名/手机号")
    parser.add_argument("--password", default=os.getenv("NUTRIDATA_PASSWORD", ""), help="nutridata.cn 密码")
    parser.add_argument("--token", default=os.getenv("NUTRIDATA_TOKEN", ""), help="已有 nutridata-token，可跳过登录")
    parser.add_argument("--output", default="nutridata_dishes.csv", help="输出 csv/xlsx 路径")
    parser.add_argument("--progress", default="nutridata_dishes_progress.jsonl", help="断点续爬进度文件")
    parser.add_argument("--page-size", type=int, default=10, help="列表接口每页数量")
    parser.add_argument("--workers", type=int, default=4, help="详情并发数，建议 2-6")
    parser.add_argument("--limit", type=int, default=0, help="仅调试前 N 条；0 表示全量")
    parser.add_argument("--delay", type=float, default=0.0, help="每个详情请求后的固定延迟秒数")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    client = NutriDataClient(token=args.token)
    if not client.token:
        if not args.username or not args.password:
            print("请通过 --username/--password 或环境变量 NUTRIDATA_USERNAME/NUTRIDATA_PASSWORD 提供登录信息。", file=sys.stderr)
            return 2
        print("正在登录 NutriData...", flush=True)
        client.login(args.username, args.password)

    db_info = client.get_db_info(DISH_DATABASE_ID)
    list_path = db_info.get("dbPath") or "dish/selectFoodList"
    detail_path = db_info.get("dbInfo") or "dish/selectFoodById"
    count_path = db_info.get("dbCount") or "dish/selectFoodCount"
    total = client.get_dish_count(count_path, DISH_DATABASE_ID)
    print(f"数据库：{db_info.get('dbName', '菜肴库')}，接口计数：{total}", flush=True)

    list_items = collect_list_items(client, list_path, total, args.page_size, args.limit)
    ids = list(list_items.keys())

    progress_path = Path(args.progress)
    done = load_progress(progress_path)
    remaining = [dish_id for dish_id in ids if dish_id not in done]
    print(f"待抓取详情：{len(remaining)}，已存在进度：{len(done)}", flush=True)

    def fetch_one(dish_id: int) -> Dict[str, Any]:
        detail = client.get_dish_detail(detail_path, dish_id, DISH_DATABASE_ID)
        row = flatten_detail(detail, list_items.get(dish_id))
        if args.delay:
            time.sleep(args.delay)
        return row

    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        future_map = {executor.submit(fetch_one, dish_id): dish_id for dish_id in remaining}
        for future in concurrent.futures.as_completed(future_map):
            dish_id = future_map[future]
            try:
                row = future.result()
            except Exception as exc:
                row = {"菜肴ID": dish_id, "食物名": clean_label(list_items.get(dish_id, {}).get("name")), "错误信息": str(exc)}
            append_progress(progress_path, row)
            done[int(dish_id)] = row
            completed += 1
            if completed % 50 == 0 or completed == len(remaining):
                ok_count = sum(1 for item in done.values() if "错误信息" not in item)
                print(f"详情进度 {completed}/{len(remaining)}，累计成功 {ok_count}/{len(ids)}", flush=True)

    ordered_rows = [done[dish_id] for dish_id in ids if dish_id in done and "错误信息" not in done[dish_id]]
    export_rows(ordered_rows, Path(args.output))
    print(f"已导出：{Path(args.output).resolve()}，成功行数：{len(ordered_rows)}", flush=True)

    error_rows = [row for row in done.values() if "错误信息" in row]
    if error_rows:
        error_path = Path(args.output).with_suffix(".errors.json")
        error_path.write_text(json.dumps(error_rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"失败记录：{error_path.resolve()}，数量：{len(error_rows)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
