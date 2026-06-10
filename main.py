import base64
import uuid
import io
import json
import logging
import os
import quopri
import re
import time
import traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from threading import Lock
from typing import List
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urljoin
from urllib.request import Request, build_opener, HTTPBasicAuthHandler, HTTPPasswordMgrWithDefaultRealm
import xml.etree.ElementTree as ET
import zipfile

import caldav
import httpx
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from supabase import create_client

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger("hsinfra")
APP_VERSION = "photo_upload_webdav_fallback_v5"

app = FastAPI()


@app.get("/version")
def version():
    return {"version": APP_VERSION}


@app.get("/health")
def health():
    return {"status": "ok", "version": APP_VERSION}


@app.on_event("startup")
def log_upload_photo_routes_on_startup():
    logger.info("[route-map] app_version=%s", APP_VERSION)
    print(f"[route-map] app_version={APP_VERSION}", flush=True)
    for route in app.routes:
        path = getattr(route, "path", "")
        if "upload-photo" not in path:
            continue
        endpoint = getattr(route, "endpoint", None)
        endpoint_name = getattr(endpoint, "__name__", str(endpoint))
        methods = sorted(getattr(route, "methods", []) or [])
        message = f"[route-map] path={path} methods={methods} endpoint={endpoint_name}"
        logger.info(message)
        print(message, flush=True)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_KEY"),
)

calendar_sync_lock = Lock()
CALENDAR_READ_ONLY_DETAIL = "Calendar is read-only from this app. Edit events in Synology Calendar and sync again."

INSPECTION_PHOTO_COLUMNS = (
    "id, inspection_id, facility_name, photo_title, file_name, storage_path, "
    "sort_order, uploaded_to_nas, local_path, local_filename, nas_folder, "
    "nas_subfolder, nas_filename, upload_status, upload_error, uploaded_at"
)
INSPECTION_PHOTO_BASE_COLUMNS = (
    "id, inspection_id, facility_name, photo_title, file_name, storage_path, "
    "sort_order, uploaded_to_nas"
)
INSPECTION_PHOTO_BASE_KEYS = {
    "inspection_id",
    "facility_name",
    "photo_title",
    "file_name",
    "storage_path",
    "sort_order",
    "uploaded_to_nas",
}
INSPECTION_PHOTO_DEFAULTS = {
    "local_path": "",
    "local_filename": "",
    "nas_folder": "",
    "nas_subfolder": "",
    "nas_filename": "",
    "upload_status": "uploaded",
    "upload_error": "",
    "uploaded_at": None,
}


class CompanyCreate(BaseModel):
    company_name: str
    address: str = ""
    address_group: str = ""
    building_type: str = ""
    manager: str = ""
    phone: str = ""
    contract_manager: str = ""
    contract_phone: str = ""
    third_manager: str = ""
    third_phone: str = ""
    contact_memo: str = ""


class InspectionPhotoUpload(BaseModel):
    facility_name: str
    photo_title: str
    file_name: str
    content_base64: str
    sort_order: int = 0


class InspectionUpload(BaseModel):
    inspection_id: str = ""
    company_name: str
    date: str
    category: str
    photos: List[InspectionPhotoUpload] = Field(default_factory=list)


class InspectionCreate(BaseModel):
    company_name: str
    inspector_name: str = ""
    date: str
    category: str
    calendar_event_uid: str = ""
    calendar_scope: str = "company_shared"
    calendar_url: str = ""
    calendar_href: str = ""
    calendar_sync_status: str = "pending"
    calendar_sync_error: str = ""
    revision: int = 0


class NasPhotoSyncTarget(BaseModel):
    company_name: str
    date: str
    category: str


class NasPhotoSyncRequest(BaseModel):
    targets: List[NasPhotoSyncTarget] = Field(default_factory=list)


class InspectionScheduleCreate(BaseModel):
    company_name: str
    date: str
    category: str
    time: str = ""


class CalendarInspector(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""


class ContactInfo(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""


class CalendarEventCreate(BaseModel):
    company_name: str
    title: str
    start_at: str
    end_at: str
    memo: str = ""
    location: str = ""
    inspector: str = ""
    inspectors: List[CalendarInspector] = Field(default_factory=list)
    all_day: bool = False
    calendar_scope: str = "company_shared"


def clean_path_segment(value: str) -> str:
    cleaned = "".join("_" if ch in '\\/:*?"<>|' else ch for ch in value).strip()
    return cleaned or "_"


def unique_file_name(file_name: str, existing_names: set[str]) -> str:
    safe_name = clean_path_segment(file_name)
    stem, extension = os.path.splitext(safe_name)
    if not stem:
        stem = "photo"
    if not extension:
        extension = ".jpg"
    if safe_name not in existing_names:
        return safe_name
    for suffix in range(2, 10000):
        candidate = f"{stem}_{suffix}{extension}"
        if candidate not in existing_names:
            return candidate
    return f"{stem}_{int(datetime.now(timezone.utc).timestamp())}{extension}"


def unique_nas_filename(
    nas_folder: str,
    nas_subfolder: str,
    file_name: str,
    nas_existing_names: set[str] | None = None,
) -> str:
    existing = (
        supabase.table("inspection_photos")
        .select("nas_filename")
        .eq("nas_folder", nas_folder)
        .eq("nas_subfolder", nas_subfolder)
        .execute()
    )
    existing_names = {
        clean_path_segment(str(row.get("nas_filename") or ""))
        for row in existing.data or []
        if str(row.get("nas_filename") or "").strip()
    }
    if nas_existing_names:
        existing_names.update(clean_path_segment(name) for name in nas_existing_names)
    return unique_file_name(file_name, existing_names)


def normalize_company_key(value: str) -> str:
    key = value.strip().lower()
    key = key.replace("㈜", "")
    key = key.replace("(주)", "")
    key = key.replace("（주）", "")
    key = key.replace("주식회사", "")
    key = re.sub(r"\s+", "", key)
    return key


def resolve_company_name_from_nas(value: str) -> str:
    target_key = normalize_company_key(value)
    if not target_key:
        return value.strip()
    try:
        companies = supabase.table("companies").select("company_name").execute()
        for company in companies.data or []:
            company_name = (company.get("company_name") or "").strip()
            if normalize_company_key(company_name) == target_key:
                return company_name
    except Exception:
        return value.strip()
    return value.strip()


def compact_date(value: str) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else clean_path_segment(value)


def normalize_phone(value: str) -> str:
    trimmed = value.strip()
    if not trimmed:
        return ""

    digits = "".join(ch for ch in trimmed if ch.isdigit())
    if digits.startswith("010") and len(digits) == 11:
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
    if len(digits) == 8:
        return f"010-{digits[:4]}-{digits[4:]}"
    if len(digits) == 10 and digits.startswith("02"):
        return f"{digits[:2]}-{digits[2:6]}-{digits[6:]}"
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return trimmed


def normalize_header(value: str) -> str:
    return re.sub(r"\s+", "", value or "").lower()


def spreadsheet_config() -> tuple[str, str, int]:
    base_url = os.getenv("SPREADSHEET_DRIVE_BASE_URL", "https://drive.hsinfra.kr").rstrip("/")
    link_id = os.getenv("SPREADSHEET_LINK_ID", "18VoRzuXn4H7vB06uBIcr0pVZQFk3cEH")
    sheet_id = int(os.getenv("SPREADSHEET_SHEET_ID", "2"))
    return base_url, link_id, sheet_id


def spreadsheet_sheet_name() -> str:
    return os.getenv("SPREADSHEET_SHEET_NAME", "업체리스트").strip() or "업체리스트"


def drive_login() -> tuple[str, str]:
    base_url, _, _ = spreadsheet_config()
    _, username, password, _ = get_filestation_config()
    response = requests.get(
        f"{base_url}/webapi/entry.cgi",
        params={
            "api": "SYNO.API.Auth",
            "version": "7",
            "method": "login",
            "account": username,
            "passwd": password,
            "session": "SynologyDrive",
            "format": "sid",
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise HTTPException(
            status_code=502,
            detail=f"Synology Drive login failed: {payload}",
        )
    return base_url, payload["data"]["sid"]


def drive_logout(base_url: str, sid: str) -> None:
    try:
        requests.get(
            f"{base_url}/webapi/entry.cgi",
            params={
                "api": "SYNO.API.Auth",
                "version": "7",
                "method": "logout",
                "session": "SynologyDrive",
                "_sid": sid,
            },
            timeout=10,
        )
    except Exception:
        pass


def get_spreadsheet_file_id(base_url: str, sid: str, link_id: str) -> str:
    response = requests.get(
        f"{base_url}/webapi/entry.cgi",
        params={
            "api": "SYNO.Office.Node",
            "version": "1",
            "method": "get",
            "basic": "true",
            "path": f"link:{link_id}",
            "_sid": sid,
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise HTTPException(
            status_code=502,
            detail=f"Synology Office node lookup failed: {payload}",
        )
    return str(payload["data"]["file_id"])


def export_spreadsheet_xlsx() -> bytes:
    base_url, link_id, _ = spreadsheet_config()
    base_url, sid = drive_login()
    try:
        file_id = os.getenv("SPREADSHEET_FILE_ID") or get_spreadsheet_file_id(
            base_url, sid, link_id
        )
        response = requests.get(
            f"{base_url}/webapi/entry.cgi/companies.xlsx",
            params={
                "api": "SYNO.Office.Export",
                "version": "1",
                "method": "download",
                "path": f"id:{file_id}",
                "_sid": sid,
            },
            timeout=60,
        )
        response.raise_for_status()
        return response.content
    finally:
        drive_logout(base_url, sid)


def xlsx_shared_strings(zip_file: zipfile.ZipFile) -> list[str]:
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    try:
        root = ET.fromstring(zip_file.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    strings = []
    for item in root.findall("m:si", ns):
        strings.append("".join(text.text or "" for text in item.findall(".//m:t", ns)))
    return strings


def column_index(cell_ref: str) -> int:
    match = re.match(r"([A-Z]+)", cell_ref)
    if not match:
        return 0
    value = 0
    for char in match.group(1):
        value = value * 26 + ord(char) - ord("A") + 1
    return value - 1


def xlsx_sheet_path(zip_file: zipfile.ZipFile, sheet_name: str) -> str | None:
    workbook_ns = {
        "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }
    rel_ns = {
        "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    }

    workbook = ET.fromstring(zip_file.read("xl/workbook.xml"))
    target_relation_id = None
    for sheet in workbook.findall("m:sheets/m:sheet", workbook_ns):
        if (sheet.get("name") or "").strip().lower() == sheet_name.strip().lower():
            target_relation_id = sheet.get(f"{{{workbook_ns['r']}}}id")
            break

    if not target_relation_id:
        return None

    relationships = ET.fromstring(zip_file.read("xl/_rels/workbook.xml.rels"))
    for relationship in relationships.findall("rel:Relationship", rel_ns):
        if relationship.get("Id") != target_relation_id:
            continue
        target = relationship.get("Target", "")
        if target.startswith("/"):
            return target.lstrip("/")
        if target.startswith("xl/"):
            return target
        return f"xl/{target}"

    return None


def read_xlsx_rows_from_path(
    zip_file: zipfile.ZipFile,
    sheet_path: str,
    shared_strings: list[str],
) -> list[list[str]]:
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    root = ET.fromstring(zip_file.read(sheet_path))
    rows = []
    for row in root.findall(".//m:row", ns):
        values: dict[int, str] = {}
        for cell in row.findall("m:c", ns):
            ref = cell.get("r", "A1")
            value_node = cell.find("m:v", ns)
            value = ""
            if value_node is not None:
                value = value_node.text or ""
                if cell.get("t") == "s":
                    value = shared_strings[int(value)]
            inline_node = cell.find("m:is/m:t", ns)
            if inline_node is not None:
                value = inline_node.text or ""
            values[column_index(ref)] = value.strip()
        if values:
            max_col = max(values)
            rows.append([values.get(index, "") for index in range(max_col + 1)])
    return rows


def read_xlsx_sheet_rows(xlsx_bytes: bytes, sheet_id: int) -> list[list[str]]:
    with zipfile.ZipFile(io.BytesIO(xlsx_bytes)) as zip_file:
        shared_strings = xlsx_shared_strings(zip_file)
        return read_xlsx_rows_from_path(
            zip_file,
            f"xl/worksheets/sheet{sheet_id}.xml",
            shared_strings,
        )


def read_xlsx_sheet_rows_by_name(xlsx_bytes: bytes, sheet_name: str) -> list[list[str]]:
    with zipfile.ZipFile(io.BytesIO(xlsx_bytes)) as zip_file:
        sheet_path = xlsx_sheet_path(zip_file, sheet_name)
        if not sheet_path:
            raise HTTPException(
                status_code=400,
                detail=f"Spreadsheet sheet '{sheet_name}' was not found.",
            )
        shared_strings = xlsx_shared_strings(zip_file)
        return read_xlsx_rows_from_path(zip_file, sheet_path, shared_strings)


def value_at(row: list[str], index: int | None) -> str:
    if index is None:
        return ""
    return row[index].strip() if index < len(row) else ""


def find_header_index(headers: list[str], aliases: list[str]) -> int | None:
    normalized_headers = {
        normalize_header(value): index for index, value in enumerate(headers)
    }
    for alias in aliases:
        normalized = normalize_header(alias)
        if normalized in normalized_headers:
            return normalized_headers[normalized]
    return None


COMPANY_SPREADSHEET_FALLBACK_COLUMNS = {
    # 순번 / HS / 구분 / 회사명 / 주소 / 점검인원 / 주소구분 / 건물유형 /
    # 점검 / 실무담당자 / 연락처1 / 계약담당자 / 연락처2 / 담당자3 / 연락처3 / 메모
    "company_name": 3,
    "address": 4,
    "address_group": 6,
    "building_type": 7,
    "manager": 9,
    "phone": 10,
    "contract_manager": 11,
    "contract_phone": 12,
    "third_manager": 13,
    "third_phone": 14,
    "contact_memo": 15,
}


def building_type_lookup_from_spreadsheet(xlsx_bytes: bytes) -> dict[str, str]:
    lookup = {}
    rows = read_xlsx_sheet_rows_by_name(xlsx_bytes, spreadsheet_sheet_name())
    if not rows:
        return lookup

    company_index = find_header_index(rows[0], ["업체명", "회사명", "업체", "회사"])
    building_type_index = find_header_index(rows[0], ["건물유형", "건물구분"])

    if company_index is None or building_type_index is None:
        return lookup

    for row in rows[1:]:
        company_name = value_at(row, company_index)
        building_type = value_at(row, building_type_index)
        if company_name and building_type:
            lookup[company_name] = building_type

    return lookup


def append_contact(
    contacts: list[str],
    seen: set[str],
    *,
    label: str,
    name: str,
    phone: str = "",
    primary_name: str = "",
    primary_phone: str = "",
) -> None:
    name = name.strip()
    phone = normalize_phone(phone)
    if not name and not phone:
        return

    key = f"{name}|{phone}"
    primary_key = f"{primary_name.strip()}|{normalize_phone(primary_phone)}"
    if key == primary_key or key in seen:
        return

    seen.add(key)
    if name and phone:
        contacts.append(f"{label}: {name} / {phone}")
    elif name:
        contacts.append(f"{label}: {name}")
    else:
        contacts.append(f"{label}: {phone}")


def contact_memo_lookup_from_spreadsheet(xlsx_bytes: bytes) -> dict[str, str]:
    lookup = {}
    rows = read_xlsx_sheet_rows_by_name(xlsx_bytes, spreadsheet_sheet_name())
    if not rows:
        return lookup

    headers = rows[0]
    company_index = find_header_index(headers, ["업체명", "회사명", "업체", "회사"])
    note_index = find_header_index(headers, ["특이사항/ 3일전협의", "특이사항", "메모"])

    if company_index is None:
        company_index = COMPANY_SPREADSHEET_FALLBACK_COLUMNS["company_name"]
    if note_index is None:
        note_index = COMPANY_SPREADSHEET_FALLBACK_COLUMNS["contact_memo"]

    for row in rows[1:]:
        company_name = value_at(row, company_index)
        if not company_name:
            continue

        note = value_at(row, note_index)
        if note:
            lookup[company_name] = note

    return lookup


def company_rows_from_spreadsheet(xlsx_bytes: bytes) -> list[dict]:
    rows = read_xlsx_sheet_rows_by_name(xlsx_bytes, spreadsheet_sheet_name())
    if not rows:
        return []

    headers = {normalize_header(value): index for index, value in enumerate(rows[0])}
    aliases = {
        "company_name": ["업체명", "회사명", "업체", "회사"],
        "address": ["주소"],
        "address_group": ["지역", "주소구분", "권역"],
        "building_type": ["건물유형", "건물구분"],
        "manager": ["실무담당자", "점검담당자", "담당자", "담당자1", "관리자"],
        "phone": ["실무담당자연락처", "실무담당자 연락처", "연락처1", "전화번호"],
        "contract_manager": ["계약담당자"],
        "contract_phone": ["계약담당자연락처", "계약담당자 연락처", "연락처2"],
        "third_manager": ["담당자3"],
        "third_phone": ["연락처3"],
    }

    column_map = {}
    for field, names in aliases.items():
        found = None
        for name in names:
            normalized = normalize_header(name)
            if normalized in headers:
                found = headers[normalized]
                break
        column_map[field] = (
            found
            if found is not None
            else COMPANY_SPREADSHEET_FALLBACK_COLUMNS[field]
        )

    building_type_lookup = building_type_lookup_from_spreadsheet(xlsx_bytes)
    contact_memo_lookup = contact_memo_lookup_from_spreadsheet(xlsx_bytes)
    companies = []
    seen = set()
    for row in rows[1:]:
        company_name = value_at(row, column_map["company_name"])
        if not company_name or company_name in seen:
            continue
        seen.add(company_name)
        building_type = value_at(row, column_map["building_type"])
        if not building_type:
            building_type = building_type_lookup.get(company_name, "")
        manager = value_at(row, column_map["manager"])
        companies.append(
            {
                "company_name": company_name,
                "address": value_at(row, column_map["address"]),
                "address_group": value_at(row, column_map["address_group"]),
                "building_type": building_type,
                "manager": manager,
                "manager_name": manager,
                "phone": normalize_phone(value_at(row, column_map["phone"])),
                "contract_manager": value_at(row, column_map["contract_manager"]),
                "contract_phone": normalize_phone(
                    value_at(row, column_map["contract_phone"])
                ),
                "third_manager": value_at(row, column_map["third_manager"]),
                "third_phone": normalize_phone(value_at(row, column_map["third_phone"])),
                "contact_memo": contact_memo_lookup.get(company_name, ""),
            }
        )
    return companies


def company_column_names() -> set[str]:
    result = supabase.table("companies").select("*").limit(1).execute()
    if not result.data:
        return {
            "company_name",
            "address",
            "address_group",
            "building_type",
            "manager",
            "phone",
            "contract_manager",
            "contract_phone",
            "third_manager",
            "third_phone",
            "contact_memo",
        }
    return set(result.data[0].keys())


def inspection_column_names() -> set[str]:
    result = supabase.table("inspections").select("*").limit(1).execute()
    if not result.data:
        return {
            "company_id",
            "date",
            "category",
            "calendar_event_uid",
            "calendar_scope",
            "calendar_url",
            "calendar_href",
            "calendar_sync_status",
            "calendar_sync_error",
            "inspector_name",
            "revision",
            "updated_at",
        }
    return set(result.data[0].keys())


def inspection_calendar_columns_available() -> bool:
    columns = inspection_column_names()
    return {
        "calendar_event_uid",
        "calendar_scope",
        "calendar_url",
        "calendar_href",
        "calendar_sync_status",
        "calendar_sync_error",
        "revision",
    }.issubset(columns)


def calendar_event_column_names() -> set[str]:
    try:
        result = supabase.table("calendar_events").select("*").limit(1).execute()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                "calendar_events table is missing or unavailable. "
                "Run the calendar_events SQL in Supabase. "
                f"Original error: {exc}"
            ),
        ) from exc
    if not result.data:
        return {
            "uid",
            "href",
            "etag",
            "calendar_scope",
            "calendar_url",
            "calendar_name",
            "company_name",
            "title",
            "description",
            "start_at",
            "end_at",
            "location",
            "inspector",
            "inspectors",
            "attendees",
            "inspection_id",
            "can_edit",
            "all_day",
            "sync_status",
            "last_synced_at",
            "deleted",
        }
    return set(result.data[0].keys())


def calendar_event_table_available() -> bool:
    try:
        calendar_event_column_names()
        return True
    except HTTPException:
        return False


def default_calendar_sync_start() -> str:
    now = datetime.now(timezone.utc)
    return (now - timedelta(days=90)).isoformat()


def default_calendar_sync_end() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=180)).isoformat()


def record_calendar_sync_run(
    *,
    status: str,
    started_at: str,
    finished_at: str,
    scopes: str,
    start_at: str,
    end_at: str,
    inserted: int = 0,
    updated: int = 0,
    deleted: int = 0,
    error_message: str = "",
) -> None:
    try:
        supabase.table("calendar_sync_runs").insert(
            {
                "status": status,
                "started_at": started_at,
                "finished_at": finished_at,
                "scopes": scopes,
                "start_at": start_at,
                "end_at": end_at,
                "inserted_count": inserted,
                "updated_count": updated,
                "deleted_count": deleted,
                "error_message": error_message,
            }
        ).execute()
    except Exception as exc:
        print(f"calendar sync run log skipped: {exc}")


def record_revision_conflict(
    inspection_id: str,
    attempted_revision: int,
    current_revision: int,
) -> None:
    try:
        supabase.table("inspection_revision_conflicts").insert(
            {
                "inspection_id": inspection_id,
                "attempted_revision": attempted_revision,
                "current_revision": current_revision,
            }
        ).execute()
    except Exception as exc:
        print(f"revision conflict log skipped: {exc}")


def table_rows(table_name: str, columns: str = "*") -> list[dict]:
    try:
        return supabase.table(table_name).select(columns).execute().data or []
    except Exception:
        return []


def latest_calendar_sync_summary() -> dict:
    try:
        rows = (
            supabase.table("calendar_sync_runs")
            .select("*")
            .order("started_at", desc=True)
            .limit(50)
            .execute()
            .data
            or []
        )
    except Exception as exc:
        return {
            "last_success_at": None,
            "last_error_message": (
                "calendar_sync_runs table is missing or unavailable. "
                "Run supabase_calendar_events.sql in Supabase. "
                f"Original error: {exc}"
            ),
            "sync_failure_count": 0,
            "last_run": None,
        }
    last_success = next((row for row in rows if row.get("status") == "success"), None)
    last_failed = next((row for row in rows if row.get("status") == "failed"), None)
    last_success_at = (last_success or {}).get("finished_at")
    last_failed_at = (last_failed or {}).get("finished_at")
    latest_error_message = ""
    if last_failed:
        if not last_success_at or str(last_failed_at or "") > str(last_success_at):
            latest_error_message = (last_failed or {}).get("error_message", "")
    return {
        "last_success_at": last_success_at,
        "last_error_message": latest_error_message,
        "sync_failure_count": sum(1 for row in rows if row.get("status") == "failed"),
        "last_run": rows[0] if rows else None,
    }


def upsert_companies(companies: list[dict]) -> dict:
    inserted = 0
    updated = 0
    allowed_columns = company_column_names()
    for company in companies:
        company_payload = {
            key: value for key, value in company.items() if key in allowed_columns
        }
        existing_select = "id"
        if "contact_memo" in allowed_columns:
            existing_select += ", contact_memo"
        existing = (
            supabase.table("companies")
            .select(existing_select)
            .eq("company_name", company["company_name"])
            .limit(1)
            .execute()
        )
        if existing.data:
            existing_row = existing.data[0]
            update_payload = {
                key: value
                for key, value in company_payload.items()
                if key == "company_name" or str(value).strip()
            }
            if (
                "contact_memo" in update_payload
                and str(existing_row.get("contact_memo") or "").strip()
            ):
                update_payload.pop("contact_memo", None)
            supabase.table("companies").update(update_payload).eq(
                "id", existing_row["id"]
            ).execute()
            updated += 1
        else:
            supabase.table("companies").insert(company_payload).execute()
            inserted += 1
    return {"inserted": inserted, "updated": updated, "total": len(companies)}


def get_nas_config() -> tuple[str, str, str]:
    base_url = os.getenv("NAS_WEBDAV_BASE_URL", "").rstrip("/")
    username = os.getenv("NAS_WEBDAV_USERNAME", "")
    password = os.getenv("NAS_WEBDAV_PASSWORD", "")

    if not base_url or not username or not password:
        raise HTTPException(
            status_code=500,
            detail=(
                "NAS WebDAV config is missing. Set NAS_WEBDAV_BASE_URL, "
                "NAS_WEBDAV_USERNAME, and NAS_WEBDAV_PASSWORD in .env."
            ),
        )

    return base_url, username, password


def get_filestation_config() -> tuple[str, str, str, str]:
    base_url = os.getenv("NAS_FILESTATION_BASE_URL", "").rstrip("/")
    username = os.getenv("NAS_WEBDAV_USERNAME", "")
    password = os.getenv("NAS_WEBDAV_PASSWORD", "")
    root_path = os.getenv("NAS_FILESTATION_ROOT_PATH", "/photo").rstrip("/")

    if not base_url:
        webdav_url_value = os.getenv("NAS_WEBDAV_BASE_URL", "").rstrip("/")
        if webdav_url_value.endswith("/photo"):
            base_url = webdav_url_value[: -len("/photo")]

    if not base_url or not username or not password or not root_path:
        raise HTTPException(
            status_code=500,
            detail=(
                "NAS File Station config is missing. Set NAS_FILESTATION_BASE_URL, "
                "NAS_FILESTATION_ROOT_PATH, NAS_WEBDAV_USERNAME, and "
                "NAS_WEBDAV_PASSWORD in .env."
            ),
        )

    return base_url, username, password, root_path


def mask_sid(sid: str) -> str:
    if not sid:
        return ""
    if len(sid) <= 8:
        return "***"
    return f"{sid[:4]}...{sid[-4:]}"


def filestation_login() -> tuple[str, str]:
    base_url, username, password, _ = get_filestation_config()
    login_params = {
        "api": "SYNO.API.Auth",
        "version": "7",
        "method": "login",
        "account": username,
        "passwd": "***",
        "session": "FileStation",
        "format": "sid",
    }
    photo_upload_log(
        f"stage=filestation_login start url={base_url}/webapi/entry.cgi, params={login_params}"
    )
    response = requests.get(
        f"{base_url}/webapi/entry.cgi",
        params={
            "api": "SYNO.API.Auth",
            "version": "7",
            "method": "login",
            "account": username,
            "passwd": password,
            "session": "FileStation",
            "format": "sid",
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()

    if not payload.get("success"):
        photo_upload_log(
            f"stage=filestation_login failed status_code={response.status_code}, response={response.text}",
            error=True,
        )
        raise HTTPException(
            status_code=502,
            detail=f"NAS File Station login failed: {payload}",
        )

    sid = payload["data"]["sid"]
    photo_upload_log(
        f"stage=filestation_login done status_code={response.status_code}, sid={mask_sid(sid)}"
    )
    return base_url, sid


def filestation_logout(base_url: str, sid: str) -> None:
    try:
        requests.get(
            f"{base_url}/webapi/entry.cgi",
            params={
                "api": "SYNO.API.Auth",
                "version": "7",
                "method": "logout",
                "session": "FileStation",
                "_sid": sid,
            },
            timeout=10,
        )
    except Exception:
        pass


def filestation_list_with_session(base_url: str, sid: str, path: str) -> list[dict]:
    params = {
        "api": "SYNO.FileStation.List",
        "version": "2",
        "method": "list",
        "folder_path": path,
        "additional": "real_path,size,time",
        "_sid": sid,
    }
    response = requests.get(
        f"{base_url}/webapi/entry.cgi",
        params=params,
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise HTTPException(
            status_code=502,
            detail=f"NAS File Station list failed: {payload}",
        )
    return payload.get("data", {}).get("files", []) or []


def filestation_log_root_path_exists(base_url: str, sid: str, root_path: str) -> None:
    try:
        filestation_list_with_session(base_url, sid, root_path)
        photo_upload_log(f"stage=filestation_root_check done root_path={root_path}")
    except Exception as exc:
        photo_upload_log(
            f"stage=filestation_root_check failed root_path={root_path}, "
            f"error_type={type(exc).__name__}, error={exc}",
            error=True,
        )
        raise


def filestation_ensure_folder(base_url: str, sid: str, parent_path: str, folder_name: str) -> None:
    safe_folder_name = clean_path_segment(folder_name)
    target_path = f"{parent_path.rstrip('/')}/{safe_folder_name}"
    params = {
        "api": "SYNO.FileStation.CreateFolder",
        "version": "2",
        "method": "create",
        "_sid": sid,
    }
    data = {
        "folder_path": parent_path,
        "name": safe_folder_name,
        "force_parent": "false",
    }
    log_params = {**params, "_sid": mask_sid(sid)}
    photo_upload_log(
        f"stage=filestation_mkdir start url={base_url}/webapi/entry.cgi, "
        f"params={log_params}, data={data}, target_path={target_path}"
    )
    response = requests.post(
        f"{base_url}/webapi/entry.cgi",
        params=params,
        data=data,
        headers={"Connection": "close"},
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("success"):
        photo_upload_log(
            f"stage=filestation_mkdir done target_path={target_path}, status_code={response.status_code}, response={response.text}"
        )
        return

    try:
        filestation_list_with_session(base_url, sid, target_path)
        photo_upload_log(
            f"stage=filestation_mkdir exists target_path={target_path}, "
            f"status_code={response.status_code}, response={response.text}"
        )
        return
    except Exception as list_exc:
        photo_upload_log(
            f"stage=filestation_mkdir failed target_path={target_path}, "
            f"status_code={response.status_code}, response={response.text}, "
            f"verify_error_type={type(list_exc).__name__}, verify_error={list_exc}",
            error=True,
        )
        raise HTTPException(
            status_code=502,
            detail=f"NAS File Station mkdir failed: {payload}",
        ) from list_exc


def upload_photo_to_filestation(
    *,
    company_name: str,
    date: str,
    category: str,
    file_name: str,
    content: bytes,
) -> str:
    base_url, sid = filestation_login()
    _, _, _, root_path = get_filestation_config()
    inspection_dir = inspection_folder_name(
        company_name=company_name,
        date=date,
        category=category,
    )
    safe_file_name = clean_path_segment(file_name)
    upload_dir = f"{root_path}/{inspection_dir}"
    nas_path = f"{upload_dir}/{safe_file_name}"

    try:
        photo_upload_log(
            f"stage=filestation_upload_prepare root_path={root_path}, upload_dir={upload_dir}, "
            f"nas_path={nas_path}, file_name={safe_file_name}, file_size={len(content)}"
        )
        filestation_log_root_path_exists(base_url, sid, root_path)
        company_folder, inspection_folder = inspection_dir.split("/", 1)
        filestation_ensure_folder(base_url, sid, root_path, company_folder)
        filestation_ensure_folder(
            base_url, sid, f"{root_path}/{company_folder}", inspection_folder
        )

        last_error: Exception | None = None
        for attempt in range(1, 4):
            params = {
                "api": "SYNO.FileStation.Upload",
                "version": "2",
                "method": "upload",
                "_sid": sid,
            }
            data = {
                "api": "SYNO.FileStation.Upload",
                "version": "2",
                "method": "upload",
                "path": upload_dir,
                "overwrite": "false",
                "_sid": sid,
            }
            log_params = {**params, "_sid": mask_sid(sid)}
            log_data = {**data, "_sid": mask_sid(sid)}
            files_log = {
                "file": {
                    "filename": safe_file_name,
                    "content_type": "image/jpeg",
                    "size": len(content),
                }
            }
            photo_upload_log(
                f"stage=filestation_upload attempt={attempt}/3 url={base_url}/webapi/entry.cgi, "
                f"params={log_params}, data={log_data}, files={files_log}, target_path={nas_path}"
            )
            try:
                response = requests.post(
                    f"{base_url}/webapi/entry.cgi",
                    params=params,
                    data=data,
                    files={
                        "file": (safe_file_name, content, "image/jpeg"),
                    },
                    headers={"Connection": "close"},
                    timeout=120,
                )
                response.raise_for_status()
                payload = response.json()

                if not payload.get("success"):
                    photo_upload_log(
                        "stage=filestation_upload response_failed "
                        f"attempt={attempt}/3, url={base_url}/webapi/entry.cgi, "
                        f"target_path={nas_path}, file_size={len(content)}, "
                        f"status_code={response.status_code}, response={response.text}, "
                        "create_parents=false; mkdir was called separately",
                        error=True,
                    )
                    raise HTTPException(
                        status_code=502,
                        detail=f"NAS File Station upload failed: {payload}",
                    )

                photo_upload_log(
                    f"stage=filestation_upload done attempt={attempt}/3, target_path={nas_path}, "
                    f"status_code={response.status_code}, response={response.text}"
                )
                return nas_path
            except HTTPException:
                raise
            except requests.RequestException as exc:
                last_error = exc
                status_code = getattr(getattr(exc, "response", None), "status_code", "")
                response_text = getattr(getattr(exc, "response", None), "text", "")
                photo_upload_log(
                    "stage=filestation_upload request_failed "
                    f"attempt={attempt}/3, url={base_url}/webapi/entry.cgi, "
                    f"target_path={nas_path}, file_size={len(content)}, "
                    f"status_code={status_code}, response={response_text}, error={exc}, "
                    "create_parents=false; mkdir was called separately",
                    error=True,
                )
        raise HTTPException(
            status_code=502,
            detail=f"NAS File Station upload failed: {last_error}",
        )
    finally:
        filestation_logout(base_url, sid)


def inspection_folder_name(*, company_name: str, date: str, category: str) -> str:
    return (
        f"{inspection_company_folder_name(company_name)}/"
        f"{inspection_subfolder_name(date=date, category=category)}"
    )


def inspection_company_folder_name(company_name: str) -> str:
    return clean_path_segment(company_name.strip() or "업체명 없음")


def inspection_subfolder_name(*, date: str, category: str) -> str:
    clean_date = normalize_inspection_date(date)
    clean_category = normalize_inspection_category(category)
    return clean_path_segment(f"{clean_category}_{clean_date}")


def normalize_inspection_date(value: str) -> str:
    text = (value or "").strip()
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", text)
    if match:
        return text
    compact = re.sub(r"[^0-9]", "", text)
    if len(compact) >= 8:
        return f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}"
    return datetime.now(timezone.utc).date().isoformat()


def inspection_folder_path(*, company_name: str, date: str, category: str) -> str:
    _, _, _, root_path = get_filestation_config()
    return f"{root_path}/{inspection_folder_name(company_name=company_name, date=date, category=category)}"


def filestation_rename_path(path: str, new_name: str) -> None:
    if not path or not path.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid NAS path.")

    base_url, sid = filestation_login()
    try:
        response = requests.get(
            f"{base_url}/webapi/entry.cgi",
            params={
                "api": "SYNO.FileStation.Rename",
                "version": "2",
                "method": "rename",
                "path": path,
                "name": new_name,
                "_sid": sid,
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            raise HTTPException(
                status_code=502,
                detail=f"NAS File Station rename failed: {payload}",
            )
    finally:
        filestation_logout(base_url, sid)


def sync_inspection_nas_folder(
    *,
    inspection_id: str,
    old_company_name: str,
    old_date: str,
    old_category: str,
    new_company_name: str,
    new_date: str,
    new_category: str,
) -> None:
    old_dir = inspection_folder_path(
        company_name=old_company_name,
        date=old_date,
        category=old_category,
    )
    new_dir_name = inspection_folder_name(
        company_name=new_company_name,
        date=new_date,
        category=new_category,
    )
    new_dir = inspection_folder_path(
        company_name=new_company_name,
        date=new_date,
        category=new_category,
    )
    if old_dir == new_dir:
        return

    photos = (
        supabase.table("inspection_photos")
        .select("id, storage_path")
        .eq("inspection_id", inspection_id)
        .execute()
    )
    if not photos.data:
        return

    filestation_rename_path(old_dir, new_dir_name)

    for photo in photos.data:
        storage_path = photo.get("storage_path") or ""
        if not storage_path.startswith(f"{old_dir}/"):
            continue
        new_storage_path = storage_path.replace(old_dir, new_dir, 1)
        supabase.table("inspection_photos").update(
            {"storage_path": new_storage_path}
        ).eq("id", photo["id"]).execute()




def download_photo_from_filestation(storage_path: str) -> bytes:
    if not storage_path or not storage_path.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid NAS storage_path.")

    base_url, sid = filestation_login()
    try:
        response = requests.get(
            f"{base_url}/webapi/entry.cgi",
            params={
                "api": "SYNO.FileStation.Download",
                "version": "2",
                "method": "download",
                "path": storage_path,
                "mode": "download",
                "_sid": sid,
            },
            timeout=60,
        )
        response.raise_for_status()
        return response.content
    finally:
        filestation_logout(base_url, sid)



def filestation_list(path: str) -> list[dict]:
    base_url, sid = filestation_login()
    try:
        response = requests.get(
            f"{base_url}/webapi/entry.cgi",
            params={
                "api": "SYNO.FileStation.List",
                "version": "2",
                "method": "list",
                "folder_path": path,
                "additional": "real_path,size,time",
                "_sid": sid,
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            raise HTTPException(
                status_code=502,
                detail=f"NAS File Station list failed: {payload}",
            )
        return payload.get("data", {}).get("files", []) or []
    finally:
        filestation_logout(base_url, sid)


def filestation_file_names(path: str) -> set[str]:
    try:
        return {
            clean_path_segment(str(item.get("name") or ""))
            for item in filestation_list(path)
            if not item.get("isdir") and str(item.get("name") or "").strip()
        }
    except HTTPException as exc:
        print(f"NAS existing file list skipped: {exc.detail}")
        return set()


def normalize_inspection_category(value: str) -> str:
    category = value.strip()
    return "유지보수" if category == "유지점검" else category


def parse_inspection_folder_name(name: str) -> dict | None:
    match = re.match(r"^(\d{8})\s+\(([^)]+)\)\s+(.+)$", name.strip())
    if not match:
        return None
    raw_date, category, company_name = match.groups()
    return {
        "date": f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}",
        "category": normalize_inspection_category(category),
        "company_name": resolve_company_name_from_nas(company_name.strip()),
    }


def parse_inspection_subfolder_name(company_name: str, name: str) -> dict | None:
    match = re.match(r"^(.+?)_(\d{4}-\d{2}-\d{2})$", name.strip())
    if not match:
        return None
    category, date = match.groups()
    return {
        "date": normalize_inspection_date(date),
        "category": normalize_inspection_category(category),
        "company_name": resolve_company_name_from_nas(company_name.strip()),
    }


def parse_nas_photo_file_name(file_name: str, fallback_order: int) -> dict | None:
    if not re.search(r"\.(jpg|jpeg|png)$", file_name, re.IGNORECASE):
        return None
    stem = re.sub(r"\.(jpg|jpeg|png)$", "", file_name, flags=re.IGNORECASE)
    match = re.match(r"^(\d+)\s*-\s*(.+?)\s*-\s*(.+)$", stem)
    if match:
        facility_no, facility_name, photo_title = match.groups()
        title_match = re.search(r"(\d+)$", photo_title.strip())
        sort_order = int(title_match.group(1)) - 1 if title_match else fallback_order
        return {
            "facility_name": facility_name.strip(),
            "photo_title": photo_title.strip(),
            "sort_order": max(sort_order, 0),
        }
    match = re.match(r"^(.+?)\s*-\s*(.+)$", stem)
    if match:
        facility_name, photo_title = match.groups()
        return {
            "facility_name": facility_name.strip(),
            "photo_title": photo_title.strip(),
            "sort_order": fallback_order,
        }
    return {
        "facility_name": "기타",
        "photo_title": stem.strip() or file_name,
        "sort_order": fallback_order,
    }


def ensure_inspection_for_nas_folder(company_name: str, date: str, category: str) -> str:
    company_id = ensure_company(company_name)
    existing = (
        supabase.table("inspections")
        .select("id")
        .eq("company_id", company_id)
        .eq("date", date)
        .eq("category", category)
        .limit(1)
        .execute()
    )
    if existing.data:
        return str(existing.data[0]["id"])
    inserted = (
        supabase.table("inspections")
        .insert({"company_id": company_id, "date": date, "category": category})
        .execute()
    )
    return str(inserted.data[0]["id"])


def sync_nas_photo_metadata(
    targets: list[NasPhotoSyncTarget] | None = None,
) -> dict:
    _, _, _, root_path = get_filestation_config()
    folders = [item for item in filestation_list(root_path) if item.get("isdir")]
    target_keys = {
        (
            target.date.strip(),
            normalize_inspection_category(target.category),
            normalize_company_key(target.company_name),
        )
        for target in targets or []
        if target.date.strip() and target.company_name.strip()
    }
    synced_folders = 0
    synced_photos = 0
    skipped_files = 0
    scanned_folders = 0

    parsed_folders = []
    for folder in folders:
        folder_name = (folder.get("name") or "").strip()
        folder_info = parse_inspection_folder_name(folder_name)
        if folder_info is not None:
            folder_key = (
                folder_info["date"],
                normalize_inspection_category(folder_info["category"]),
                normalize_company_key(folder_info["company_name"]),
            )
            if target_keys and folder_key not in target_keys:
                continue
            parsed_folders.append((folder, folder_info, folder_name))
            continue

        company_folder_name = folder_name
        company_folder_path = folder.get("path") or f"{root_path}/{company_folder_name}"
        try:
            subfolders = [
                item for item in filestation_list(company_folder_path) if item.get("isdir")
            ]
        except Exception:
            continue
        for subfolder in subfolders:
            subfolder_name = (subfolder.get("name") or "").strip()
            folder_info = parse_inspection_subfolder_name(
                company_folder_name,
                subfolder_name,
            )
            if folder_info is None:
                continue
            folder_key = (
                folder_info["date"],
                normalize_inspection_category(folder_info["category"]),
                normalize_company_key(folder_info["company_name"]),
            )
            if target_keys and folder_key not in target_keys:
                continue
            parsed_folders.append((subfolder, folder_info, f"{company_folder_name}/{subfolder_name}"))

    if not target_keys:
        parsed_folders = parsed_folders[-30:]

    for folder, folder_info, folder_name in parsed_folders:
        scanned_folders += 1

        folder_path = folder.get("path") or f"{root_path}/{folder_name}"
        inspection_id = ensure_inspection_for_nas_folder(
            folder_info["company_name"],
            folder_info["date"],
            folder_info["category"],
        )
        files = [item for item in filestation_list(folder_path) if not item.get("isdir")]
        fallback_order_by_facility: dict[str, int] = {}

        for file in files:
            file_name = (file.get("name") or "").strip()
            parsed = parse_nas_photo_file_name(
                file_name,
                fallback_order_by_facility.get("기타", 0),
            )
            if parsed is None:
                skipped_files += 1
                continue
            facility_name = parsed["facility_name"]
            if parsed["sort_order"] == fallback_order_by_facility.get("기타", 0):
                parsed["sort_order"] = fallback_order_by_facility.get(facility_name, 0)
            fallback_order_by_facility[facility_name] = parsed["sort_order"] + 1

            storage_path = file.get("path") or f"{folder_path}/{file_name}"
            nas_folder = inspection_company_folder_name(folder_info["company_name"])
            nas_subfolder = inspection_subfolder_name(
                date=folder_info["date"],
                category=folder_info["category"],
            )
            supabase.table("inspection_photos").upsert(
                {
                    "inspection_id": inspection_id,
                    "facility_name": facility_name,
                    "photo_title": parsed["photo_title"],
                    "file_name": file_name,
                    "storage_path": storage_path,
                    "sort_order": parsed["sort_order"],
                    "uploaded_to_nas": True,
                    "nas_folder": nas_folder,
                    "nas_subfolder": nas_subfolder,
                    "nas_filename": file_name,
                    "upload_status": "uploaded",
                    "upload_error": "",
                    "uploaded_at": datetime.now(timezone.utc).isoformat(),
                },
                on_conflict="inspection_id,facility_name,sort_order",
            ).execute()
            synced_photos += 1

        synced_folders += 1

    return {
        "scanned_folders": scanned_folders,
        "matched_targets": len(target_keys),
        "synced_folders": synced_folders,
        "synced_photos": synced_photos,
        "skipped_files": skipped_files,
    }

def webdav_url(base_url: str, relative_path: str) -> str:
    parts = [quote(part, safe="") for part in relative_path.split("/") if part]
    if not parts:
        return base_url
    return f"{base_url}/{'/'.join(parts)}"


def webdav_opener(base_url: str, username: str, password: str):
    password_mgr = HTTPPasswordMgrWithDefaultRealm()
    password_mgr.add_password(None, base_url, username, password)
    auth_handler = HTTPBasicAuthHandler(password_mgr)
    return build_opener(auth_handler)


def webdav_request(
    method: str,
    relative_path: str,
    data: bytes | None = None,
    content_type: str | None = None,
    *,
    success_codes: set[int] | None = None,
    existing_codes: set[int] | None = None,
    stage: str = "webdav_request",
    target_path: str = "",
) -> tuple[int, str]:
    base_url, username, password = get_nas_config()
    target_url = webdav_url(base_url, relative_path)
    headers = {"Connection": "close"}
    if content_type:
        headers["Content-Type"] = content_type
    success_codes = success_codes or {200, 201, 204}
    existing_codes = existing_codes or set()

    photo_upload_log(
        f"stage={stage} start method={method}, target_url={target_url}, "
        f"target_path={target_path or relative_path}, bytes={len(data or b'')}"
    )
    try:
        response = requests.request(
            method,
            target_url,
            data=data,
            headers=headers,
            auth=(username, password),
            timeout=120,
        )
    except requests.RequestException as exc:
        photo_upload_log(
            f"stage={stage} failed method={method}, target_url={target_url}, "
            f"target_path={target_path or relative_path}, error_type={type(exc).__name__}, error={exc}",
            error=True,
        )
        raise HTTPException(
            status_code=502,
            detail=f"NAS WebDAV {method} failed: {exc}",
        ) from exc

    response_text = response.text
    if response.status_code in success_codes:
        photo_upload_log(
            f"stage={stage} done method={method}, target_url={target_url}, "
            f"target_path={target_path or relative_path}, status_code={response.status_code}, "
            f"response={response_text}"
        )
        return response.status_code, response_text

    if response.status_code in existing_codes:
        photo_upload_log(
            f"stage={stage} exists method={method}, target_url={target_url}, "
            f"target_path={target_path or relative_path}, status_code={response.status_code}, "
            f"response={response_text}"
        )
        return response.status_code, response_text

    photo_upload_log(
        f"stage={stage} failed method={method}, target_url={target_url}, "
        f"target_path={target_path or relative_path}, status_code={response.status_code}, "
        f"response={response_text}",
        error=True,
    )
    raise HTTPException(
        status_code=502,
        detail=f"NAS WebDAV {method} failed: HTTP {response.status_code}: {response_text}",
    )


def ensure_nas_dirs(relative_dir: str, *, nas_dir: str = "") -> None:
    current = ""
    nas_current = ""
    for part in [item for item in relative_dir.split("/") if item]:
        current = f"{current}/{part}" if current else part
        nas_current = f"{nas_current}/{part}" if nas_current else part
        target_path = f"{nas_dir.rstrip('/')}/{nas_current}" if nas_dir else current
        webdav_request(
            "MKCOL",
            current,
            success_codes={200, 201, 204},
            existing_codes={405, 409},
            stage="webdav_mkdir",
            target_path=target_path,
        )


def upload_photo_to_webdav(
    *,
    company_name: str,
    date: str,
    category: str,
    file_name: str,
    content: bytes,
) -> str:
    _, _, _, root_path = get_filestation_config()
    inspection_dir = inspection_folder_name(
        company_name=company_name,
        date=date,
        category=category,
    )
    safe_file_name = clean_path_segment(file_name)
    relative_path = f"{inspection_dir}/{safe_file_name}"
    nas_path = f"{root_path}/{relative_path}"

    ensure_nas_dirs(inspection_dir, nas_dir=root_path)
    webdav_request(
        "PUT",
        relative_path,
        data=content,
        content_type="image/jpeg",
        success_codes={200, 201, 204},
        stage="webdav_upload",
        target_path=nas_path,
    )
    return nas_path


def upload_photo_to_nas(
    *,
    company_name: str,
    date: str,
    category: str,
    file_name: str,
    content: bytes,
) -> str:
    try:
        return upload_photo_to_filestation(
            company_name=company_name,
            date=date,
            category=category,
            file_name=file_name,
            content=content,
        )
    except Exception as filestation_exc:
        filestation_error = (
            filestation_exc.detail
            if isinstance(filestation_exc, HTTPException)
            else str(filestation_exc)
        )
        photo_upload_log(
            f"stage=webdav_fallback start reason=file_station_failed, "
            f"error_type={type(filestation_exc).__name__}, error={filestation_error}",
            error=True,
        )
        try:
            nas_path = upload_photo_to_webdav(
                company_name=company_name,
                date=date,
                category=category,
                file_name=file_name,
                content=content,
            )
            photo_upload_log(
                f"stage=webdav_fallback done status=uploaded, nas_path={nas_path}, "
                f"file_station_error={filestation_error}"
            )
            return nas_path
        except Exception as webdav_exc:
            webdav_error = (
                webdav_exc.detail
                if isinstance(webdav_exc, HTTPException)
                else str(webdav_exc)
            )
            photo_upload_log(
                f"stage=webdav_fallback failed file_station_error={filestation_error}, "
                f"webdav_error_type={type(webdav_exc).__name__}, webdav_error={webdav_error}",
                error=True,
            )
            raise HTTPException(
                status_code=502,
                detail=(
                    "NAS upload failed: "
                    f"File Station error={filestation_error}; "
                    f"WebDAV error={webdav_error}"
                ),
            ) from webdav_exc


def normalize_inspection_photo_row(row: dict) -> dict:
    normalized = {**INSPECTION_PHOTO_DEFAULTS, **(row or {})}
    if not str(normalized.get("nas_filename") or "").strip():
        normalized["nas_filename"] = str(normalized.get("file_name") or "")
    return normalized


def inspection_photo_base_payload(photo_payload: dict) -> dict:
    return {
        key: value
        for key, value in photo_payload.items()
        if key in INSPECTION_PHOTO_BASE_KEYS
    }


def select_inspection_photos(query_builder):
    try:
        result = query_builder(INSPECTION_PHOTO_COLUMNS).execute()
    except Exception as exc:
        print(f"inspection_photos full column select fallback: {exc}")
        result = query_builder(INSPECTION_PHOTO_BASE_COLUMNS).execute()
    return [normalize_inspection_photo_row(row) for row in result.data or []]


def upsert_inspection_photo_metadata(photo_payload: dict) -> tuple[dict, bool, str]:
    try:
        inserted_photo = (
            supabase.table("inspection_photos")
            .upsert(
                photo_payload,
                on_conflict="inspection_id,facility_name,sort_order",
            )
            .execute()
        )
        return (
            normalize_inspection_photo_row(inserted_photo.data[0])
            if inserted_photo.data
            else {},
            True,
            "",
        )
    except Exception as exc:
        metadata_error = str(exc)
        base_payload = inspection_photo_base_payload(photo_payload)
        try:
            existing_photo = (
                supabase.table("inspection_photos")
                .select("id")
                .eq("inspection_id", photo_payload["inspection_id"])
                .eq("facility_name", photo_payload["facility_name"])
                .eq("sort_order", photo_payload["sort_order"])
                .limit(1)
                .execute()
            )
            if existing_photo.data:
                photo_id = existing_photo.data[0]["id"]
                updated_photo = (
                    supabase.table("inspection_photos")
                    .update(base_payload)
                    .eq("id", photo_id)
                    .execute()
                )
                row = updated_photo.data[0] if updated_photo.data else {"id": photo_id}
                return normalize_inspection_photo_row(row), True, metadata_error

            inserted_photo = (
                supabase.table("inspection_photos").insert(base_payload).execute()
            )
            row = inserted_photo.data[0] if inserted_photo.data else {}
            return normalize_inspection_photo_row(row), True, metadata_error
        except Exception as fallback_exc:
            return {}, False, f"{metadata_error}; fallback failed: {fallback_exc}"




def photo_upload_log(message: str, *, error: bool = False) -> None:
    text = f"[photo-upload] {message}"
    if error:
        logger.error(text)
    else:
        logger.info(text)
    print(text, flush=True)

def is_supabase_transient_error(exc: Exception) -> bool:
    error_type = type(exc).__name__
    message = str(exc).lower()
    return (
        isinstance(exc, (httpx.ReadError, httpx.TimeoutException, httpx.TransportError))
        or "readerror" in error_type.lower()
        or "timeouterror" in error_type.lower()
        or "timeout" in message
        or "server disconnected" in message
        or "connection reset" in message
    )


def run_supabase_step_with_retry(
    *,
    stage: str,
    operation,
    inspection_id: str = "",
    company_id: str = "",
    max_attempts: int = 3,
):
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            error_type = type(exc).__name__
            log_text = (
                f"[photo-upload] stage={stage}, attempt={attempt}/{max_attempts}, "
                f"inspection_id={inspection_id}, company_id={company_id}, "
                f"error_type={error_type}, error={exc}"
            )
            logger.warning(log_text)
            print(log_text, flush=True)
            if attempt >= max_attempts or not is_supabase_transient_error(exc):
                raise
            time.sleep(0.6 * attempt)
    raise last_error or RuntimeError(f"{stage} failed")

def ensure_company(company_name: str) -> str:
    existing = (
        supabase.table("companies")
        .select("id")
        .eq("company_name", company_name)
        .limit(1)
        .execute()
    )

    if existing.data:
        return str(existing.data[0]["id"])

    inserted = (
        supabase.table("companies")
        .insert(
            {
                "company_name": company_name,
                "address": "",
                "address_group": "",
                "building_type": "",
                "manager": "",
                "phone": "",
            }
        )
        .execute()
    )

    return str(inserted.data[0]["id"])


def create_inspection(company_id: str, inspection: InspectionUpload) -> str:
    inspection_id = inspection.inspection_id.strip()
    if inspection_id:
        run_supabase_step_with_retry(
            stage="create_inspection/update",
            inspection_id=inspection_id,
            company_id=company_id,
            operation=lambda: supabase.table("inspections")
            .update(
                {
                    "company_id": company_id,
                    "date": inspection.date,
                    "category": inspection.category,
                }
            )
            .eq("id", inspection_id)
            .execute(),
        )
        return inspection_id

    existing = run_supabase_step_with_retry(
        stage="create_inspection/select_existing",
        inspection_id=inspection_id,
        company_id=company_id,
        operation=lambda: supabase.table("inspections")
        .select("id")
        .eq("company_id", company_id)
        .eq("date", inspection.date)
        .eq("category", inspection.category)
        .limit(1)
        .execute(),
    )
    if existing.data:
        return str(existing.data[0]["id"])

    inserted = run_supabase_step_with_retry(
        stage="create_inspection/insert",
        inspection_id=inspection_id,
        company_id=company_id,
        operation=lambda: supabase.table("inspections")
        .insert(
            {
                "company_id": company_id,
                "date": inspection.date,
                "category": inspection.category,
            }
        )
        .execute(),
    )

    return str(inserted.data[0]["id"])


def inspection_calendar_event_payload(inspection: InspectionCreate) -> CalendarEventCreate:
    company_name = inspection.company_name.strip()
    category = normalize_inspection_category(inspection.category)
    title = f"({category}) {company_name}".strip()
    return CalendarEventCreate(
        company_name=company_name,
        title=title,
        start_at=inspection.date,
        end_at=inspection.date,
        memo=f"업체명: {company_name}".strip(),
        location=company_address_for_calendar(company_name),
        inspectors=[],
        all_day=True,
        calendar_scope=normalize_calendar_scope(inspection.calendar_scope),
    )


def normalize_calendar_sync_status(value: str) -> str:
    status = (value or "pending").strip().lower()
    if status in {"pending", "synced", "failed"}:
        return status
    return "pending"


def inspection_payload_from_create(inspection: InspectionCreate) -> dict:
    company_name = inspection.company_name.strip()
    if not company_name:
        raise HTTPException(status_code=400, detail="company_name is required.")
    company_id = ensure_company(company_name)
    payload = {
        "company_id": company_id,
        "date": inspection.date,
        "category": inspection.category,
        "inspector_name": inspection.inspector_name.strip(),
        "calendar_event_uid": inspection.calendar_event_uid.strip(),
        "calendar_scope": normalize_calendar_scope(inspection.calendar_scope),
        "calendar_url": inspection.calendar_url.strip(),
        "calendar_href": inspection.calendar_href.strip(),
        "calendar_sync_status": normalize_calendar_sync_status(
            inspection.calendar_sync_status
        ),
        "calendar_sync_error": inspection.calendar_sync_error.strip(),
    }
    allowed_columns = inspection_column_names()
    return {key: value for key, value in payload.items() if key in allowed_columns}


def find_calendar_event_uid_by_href(
    *,
    calendar_href: str,
    calendar_scope: str,
    date: str,
) -> str:
    href = calendar_href.strip()
    if not href:
        return ""
    try:
        entry = calendar_entry_for_scope(calendar_scope)
        start_at = parse_event_datetime(date)
        end_at = start_at + timedelta(days=2)
        events = entry["calendar"].date_search(start=start_at, end=end_at)
        for event in events:
            event_href = caldav_event_attr(event, "url")
            if event_href == href:
                return calendar_event_to_json(event, entry).get("uid", "")
    except Exception:
        return ""
    return ""


def update_inspection_calendar_fields(inspection_id: str, payload: dict) -> dict:
    if not payload:
        return {}
    allowed_columns = inspection_column_names()
    filtered = {key: value for key, value in payload.items() if key in allowed_columns}
    if filtered:
        supabase.table("inspections").update(filtered).eq("id", inspection_id).execute()
    return filtered


def sync_calendar_event_to_inspection(
    inspection_id: str,
    inspection: InspectionCreate,
    existing_row: dict | None = None,
    *,
    allow_create: bool,
) -> dict:
    if not inspection_calendar_columns_available():
        return {}
    row = existing_row or {}
    scope = normalize_calendar_scope(
        inspection.calendar_scope or str(row.get("calendar_scope") or "company_shared")
    )
    existing_uid = (
        inspection.calendar_event_uid.strip()
        or str(row.get("calendar_event_uid") or "").strip()
    )
    href = (
        inspection.calendar_href.strip()
        or str(row.get("calendar_href") or "").strip()
    )
    if not existing_uid and href:
        existing_uid = find_calendar_event_uid_by_href(
            calendar_href=href,
            calendar_scope=scope,
            date=inspection.date,
        )

    if not existing_uid and not allow_create:
        return update_inspection_calendar_fields(
            inspection_id,
            {
                "calendar_sync_status": "pending",
                "calendar_sync_error": (
                    "calendar_event_uid가 없어 중복 생성을 방지하기 위해 "
                    "캘린더 이벤트 생성을 보류했습니다."
                ),
            },
        )

    event_payload = inspection_calendar_event_payload(inspection)
    event_payload.calendar_scope = scope

    try:
        if existing_uid:
            event_result = update_synology_calendar_event(existing_uid, event_payload)
        else:
            event_result = create_synology_calendar_event(
                event_payload,
                calendar_scope=scope,
            )
    except Exception as exc:
        return update_inspection_calendar_fields(
            inspection_id,
            {
                "calendar_sync_status": "failed",
                "calendar_sync_error": str(exc),
            },
        )

    calendar_fields = {
        "calendar_event_uid": event_result.get("uid", existing_uid),
        "calendar_scope": event_result.get("calendar_scope", scope),
        "calendar_url": event_result.get("calendar_url", ""),
        "calendar_href": event_result.get("href", event_result.get("url", "")),
        "calendar_sync_status": "synced",
        "calendar_sync_error": "",
    }
    updated_fields = update_inspection_calendar_fields(inspection_id, calendar_fields)
    upsert_calendar_event_for_inspection(inspection_id, event_result, event_payload)
    return updated_fields



def caldav_config() -> tuple[str, str, str]:
    url = os.getenv("SYNOLOGY_CALDAV_URL", "").strip()
    username = os.getenv("SYNOLOGY_CALDAV_USERNAME", "").strip()
    password = os.getenv("SYNOLOGY_CALDAV_PASSWORD", "").strip()
    if not url or not username or not password:
        raise HTTPException(
            status_code=503,
            detail=(
                "Synology CalDAV config is missing. Set "
                "SYNOLOGY_CALDAV_URL, SYNOLOGY_CALDAV_USERNAME, "
                "and SYNOLOGY_CALDAV_PASSWORD."
            ),
        )
    return url, username, password


def carddav_config() -> tuple[str, str, str]:
    url = (
        os.getenv("SYNOLOGY_CARDDAV_URL", "")
        or os.getenv("CONTACTS_CARDDAV_URL", "")
        or "https://hsinfra.kr:5031/carddav/tech11/62daf836-5d56-4be4-a981-5f85e7fe7003"
    ).strip()
    username = (
        os.getenv("SYNOLOGY_CARDDAV_USERNAME", "")
        or os.getenv("CONTACTS_CARDDAV_USERNAME", "")
        or os.getenv("SYNOLOGY_CALDAV_USERNAME", "")
    ).strip()
    password = (
        os.getenv("SYNOLOGY_CARDDAV_PASSWORD", "")
        or os.getenv("CONTACTS_CARDDAV_PASSWORD", "")
        or os.getenv("SYNOLOGY_CALDAV_PASSWORD", "")
    ).strip()
    if not url or not username or not password:
        raise HTTPException(
            status_code=503,
            detail=(
                "Synology CardDAV config is missing. Set SYNOLOGY_CARDDAV_URL, "
                "SYNOLOGY_CARDDAV_USERNAME, and SYNOLOGY_CARDDAV_PASSWORD. "
                "CALDAV credentials are used as a fallback."
            ),
        )
    return url.rstrip("/") + "/", username, password


def unfold_vcard(value: str) -> str:
    return re.sub(r"\r?\n[ \t]", "", value.replace("\r\n", "\n"))


def decode_vcard_value(value: str, params: str) -> str:
    raw = value.strip()
    if "QUOTED-PRINTABLE" in params.upper():
        try:
            raw = quopri.decodestring(raw).decode("utf-8")
        except Exception:
            try:
                raw = quopri.decodestring(raw).decode("cp949")
            except Exception:
                pass
    return (
        raw.replace("\\n", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .strip()
    )


def parse_vcard(vcard: str) -> ContactInfo | None:
    data = unfold_vcard(vcard)
    name = ""
    email = ""
    phone = ""

    for line in data.splitlines():
        if ":" not in line:
            continue
        left, value = line.split(":", 1)
        field = left.split(";", 1)[0].upper()
        params = left.upper()
        decoded = decode_vcard_value(value, params)
        if field == "FN" and not name:
            name = decoded
        elif field == "N" and not name:
            parts = [part for part in decoded.split(";") if part.strip()]
            name = " ".join(reversed(parts[:2])).strip() if len(parts) >= 2 else decoded
        elif field == "EMAIL" and not email:
            email = decoded
        elif field == "TEL" and not phone:
            phone = decoded

    if not name and email:
        name = email
    if not name and not email and not phone:
        return None
    return ContactInfo(name=name, email=email, phone=phone)


def carddav_contact_cards() -> list[str]:
    url, username, password = carddav_config()
    body = '''<?xml version="1.0" encoding="utf-8" ?>
<card:addressbook-query xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
  <d:prop>
    <card:address-data />
  </d:prop>
</card:addressbook-query>'''

    response = requests.request(
        "REPORT",
        url,
        auth=(username, password),
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        data=body.encode("utf-8"),
        timeout=25,
    )
    if response.status_code not in (207, 200):
        raise HTTPException(
            status_code=502,
            detail=f"Synology CardDAV search failed: HTTP {response.status_code} {response.text[:300]}",
        )

    root = ET.fromstring(response.content)
    ns = {"card": "urn:ietf:params:xml:ns:carddav"}
    return [
        (node.text or "").strip()
        for node in root.findall(".//card:address-data", ns)
        if (node.text or "").strip()
    ]


def search_carddav_contacts(q: str = "", limit: int = 30) -> list[dict]:
    keyword = q.strip().lower()
    contacts: list[ContactInfo] = []
    seen: set[tuple[str, str, str]] = set()
    for card in carddav_contact_cards():
        contact = parse_vcard(card)
        if contact is None:
            continue
        haystack = " ".join([contact.name, contact.email, contact.phone]).lower()
        if keyword and keyword not in haystack:
            continue
        key = (contact.name.lower(), contact.email.lower(), contact.phone.lower())
        if key in seen:
            continue
        seen.add(key)
        contacts.append(contact)
        if len(contacts) >= limit:
            break
    return [contact.dict() for contact in contacts]


CONTACT_EMAIL_NAME_CACHE: dict[str, str] = {}
CONTACT_EMAIL_NAME_CACHE_LOADED = False


def contact_email_name_map() -> dict[str, str]:
    global CONTACT_EMAIL_NAME_CACHE, CONTACT_EMAIL_NAME_CACHE_LOADED
    if CONTACT_EMAIL_NAME_CACHE_LOADED:
        return CONTACT_EMAIL_NAME_CACHE
    mapping: dict[str, str] = {}
    try:
        for card in carddav_contact_cards():
            contact = parse_vcard(card)
            if contact is None:
                continue
            email = contact.email.strip().lower()
            name = contact.name.strip()
            if email and name and name.lower() != email:
                mapping[email] = name
    except Exception as exc:
        print(f"CardDAV attendee name enrichment skipped: {exc}")
    CONTACT_EMAIL_NAME_CACHE = mapping
    CONTACT_EMAIL_NAME_CACHE_LOADED = True
    return mapping


def contact_name_for_email(email: str) -> str:
    return contact_email_name_map().get(email.strip().lower(), "")



def synology_calendar_by_display_name(
    base_url: str,
    username: str,
    password: str,
    calendar_name: str,
):
    target = calendar_name.strip()
    if not target:
        return None

    collection_url = f"{base_url.rstrip('/')}/{username}/"
    body = """<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop>
    <d:displayname />
    <d:resourcetype />
  </d:prop>
</d:propfind>"""

    response = requests.request(
        "PROPFIND",
        collection_url,
        auth=(username, password),
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        data=body.encode("utf-8"),
        timeout=20,
    )
    if response.status_code not in (207, 200):
        raise HTTPException(
            status_code=502,
            detail=(
                "Synology CalDAV calendar list failed: "
                f"HTTP {response.status_code} {response.text[:300]}"
            ),
        )

    root = ET.fromstring(response.content)
    ns = {
        "d": "DAV:",
        "c": "urn:ietf:params:xml:ns:caldav",
    }

    for item in root.findall("d:response", ns):
        href = (item.findtext("d:href", default="", namespaces=ns) or "").strip()
        display_name = repair_mojibake_text(
            item.findtext(".//d:displayname", default="", namespaces=ns) or ""
        )
        resource_type = item.find(".//d:resourcetype", ns)
        is_calendar = resource_type is not None and resource_type.find("c:calendar", ns) is not None
        decoded_href = unquote(href).rstrip("/")

        if not is_calendar:
            continue
        if display_name != target and not decoded_href.endswith("/" + target):
            continue

        calendar_url = urljoin(collection_url, href)
        client = caldav.DAVClient(
            url=calendar_url,
            username=username,
            password=password,
        )
        return client.calendar(url=calendar_url)

    return None



def configured_shared_calendar_names() -> set[str]:
    raw = os.getenv(
        "SYNOLOGY_CALDAV_SHARED_NAMES",
        os.getenv("SYNOLOGY_CALDAV_SHARED_NAME", os.getenv("SYNOLOGY_CALDAV_CALENDAR_NAME", "점검")),
    )
    names = {item.strip() for item in raw.split(",") if item.strip()}
    names.add("점검")
    return names


def configured_personal_calendar_names() -> set[str]:
    raw = os.getenv("SYNOLOGY_CALDAV_PERSONAL_NAMES", "My Calendar,home")
    return {item.strip() for item in raw.split(",") if item.strip()}


def repair_mojibake_text(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    try:
        repaired = text.encode("latin1").decode("utf-8")
        if repaired and repaired != text:
            return repaired
    except Exception:
        pass
    return text


def calendar_display_name(calendar) -> str:
    for attr in ("name", "display_name"):
        try:
            value = getattr(calendar, attr, "")
            if callable(value):
                value = value()
            value = repair_mojibake_text(str(value or ""))
            if value:
                return value
        except Exception:
            pass
    url_tail = repair_mojibake_text(
        unquote(str(getattr(calendar, "url", "")).rstrip("/").split("/")[-1])
    )
    return url_tail or "Calendar"


def normalize_calendar_scope(scope: str) -> str:
    normalized = (scope or "company_shared").strip().lower()
    if normalized in {"shared", "company", "company_shared"}:
        return "company_shared"
    if normalized in {"personal", "my", "mine"}:
        return "personal"
    if normalized == "other":
        return "other"
    return "company_shared"


def calendar_scope_for(name: str, url: str) -> str:
    decoded_url = unquote(url).rstrip("/")
    tail = decoded_url.split("/")[-1]
    shared_names = configured_shared_calendar_names()
    personal_names = configured_personal_calendar_names()
    if name in shared_names or tail in shared_names:
        return "company_shared"
    if name in personal_names or tail in personal_names:
        return "personal"
    if tail == "home" or name.lower() in {"my calendar", "personal"}:
        return "personal"
    return "other"


def calendar_can_write(calendar, username: str, password: str) -> bool:
    url = str(getattr(calendar, "url", "")).rstrip("/") + "/"
    body = """<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="DAV:">
  <d:prop>
    <d:current-user-privilege-set />
  </d:prop>
</d:propfind>"""
    try:
        response = requests.request(
            "PROPFIND",
            url,
            auth=(username, password),
            headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
            data=body.encode("utf-8"),
            timeout=15,
        )
        if response.status_code not in (207, 200):
            return True
        root = ET.fromstring(response.content)
        privileges = [
            item.tag.split("}", 1)[-1]
            for item in root.findall(".//{DAV:}privilege/*")
        ]
        if not privileges:
            return True
        return any(item in {"write", "write-content", "bind", "unbind", "all"} for item in privileges)
    except Exception:
        return True


def calendar_entry_debug(entry: dict) -> dict:
    return {
        "scope": entry.get("scope", ""),
        "name": entry.get("name", ""),
        "url": entry.get("url", ""),
        "can_write": bool(entry.get("can_write")),
    }


def log_calendar_debug(message: str, payload: dict):
    try:
        print(f"[calendar-debug] {message}: {json.dumps(payload, ensure_ascii=False, default=str)}")
    except Exception:
        print(f"[calendar-debug] {message}: {payload}")


def synology_calendar_entries() -> list[dict]:
    url, username, password = caldav_config()
    client = caldav.DAVClient(url=url, username=username, password=password)
    calendars = []
    try:
        calendars = client.principal().calendars()
    except Exception:
        calendars = []

    entries: list[dict] = []
    seen_urls: set[str] = set()

    for calendar in calendars:
        calendar_url = str(getattr(calendar, "url", "")).rstrip("/") + "/"
        if not calendar_url or calendar_url in seen_urls:
            continue
        try:
            supported = calendar.get_supported_components()
            if supported and "VEVENT" not in supported:
                continue
        except Exception:
            pass
        name = calendar_display_name(calendar)
        scope = calendar_scope_for(name, calendar_url)
        entries.append(
            {
                "scope": scope,
                "name": name,
                "url": calendar_url,
                "can_write": calendar_can_write(calendar, username, password),
                "calendar": calendar,
            }
        )
        seen_urls.add(calendar_url)

    target_calendar_name = os.getenv("SYNOLOGY_CALDAV_CALENDAR_NAME", "점검").strip()
    if target_calendar_name and not any(item["scope"] == "company_shared" for item in entries):
        found = synology_calendar_by_display_name(url.rstrip("/"), username, password, target_calendar_name)
        if found is not None:
            calendar_url = str(getattr(found, "url", "")).rstrip("/") + "/"
            if calendar_url not in seen_urls:
                entries.append(
                    {
                        "scope": "company_shared",
                        "name": calendar_display_name(found),
                        "url": calendar_url,
                        "can_write": calendar_can_write(found, username, password),
                        "calendar": found,
                    }
                )

    entries.sort(key=lambda item: (0 if item["scope"] == "personal" else 1 if item["scope"] == "company_shared" else 2, item["name"]))
    log_calendar_debug(
        "discovery",
        {
            "configured_url": url,
            "shared_names": sorted(configured_shared_calendar_names()),
            "personal_names": sorted(configured_personal_calendar_names()),
            "calendars": [calendar_entry_debug(item) for item in entries],
        },
    )
    return entries


def public_calendar_entries() -> list[dict]:
    return [
        {
            "scope": item["scope"],
            "name": item["name"],
            "url": item["url"],
            "calendarName": item["name"],
            "calendarUrl": item["url"],
            "can_write": item["can_write"],
            "canWrite": item["can_write"],
            "read_only": not item["can_write"],
            "readOnly": not item["can_write"],
        }
        for item in synology_calendar_entries()
    ]


def calendar_entry_for_scope(scope: str) -> dict:
    normalized = normalize_calendar_scope(scope)
    if normalized == "other":
        normalized = "company_shared"
    entries = synology_calendar_entries()
    for item in entries:
        if item["scope"] == normalized:
            return item
    if entries:
        return entries[0]
    raise HTTPException(status_code=502, detail="Synology CalDAV calendar list is empty.")

def synology_calendar():
    url, username, password = caldav_config()
    target_calendar_name = os.getenv("SYNOLOGY_CALDAV_CALENDAR_NAME", "점검").strip()
    base_url = url.rstrip("/")
    candidate_urls = []

    def add_candidate(candidate: str):
        normalized = candidate.rstrip("/") + "/"
        if normalized not in candidate_urls:
            candidate_urls.append(normalized)

    add_candidate(url)
    if target_calendar_name:
        add_candidate(f"{base_url}/{username}/{quote(target_calendar_name, safe='')}/")
    add_candidate(f"{base_url}/{username}/home/")
    add_candidate(f"{base_url}/{username}/")

    try:
        propfind_error = None
        if target_calendar_name:
            try:
                calendar = synology_calendar_by_display_name(
                    base_url,
                    username,
                    password,
                    target_calendar_name,
                )
                if calendar is not None:
                    return calendar
            except HTTPException:
                raise
            except Exception as exc:
                propfind_error = exc

        last_candidate_error = None
        for candidate_url in candidate_urls:
            try:
                candidate_client = caldav.DAVClient(
                    url=candidate_url,
                    username=username,
                    password=password,
                )
                calendar = candidate_client.calendar(url=candidate_url)
                probe_start = datetime.now()
                calendar.date_search(
                    start=probe_start,
                    end=probe_start + timedelta(seconds=1),
                )
                return calendar
            except Exception as exc:
                last_candidate_error = f"{candidate_url}: {exc}"

        client = caldav.DAVClient(url=url, username=username, password=password)
        discovery_error = None
        try:
            calendars = client.principal().calendars()
            if calendars:
                if target_calendar_name:
                    for cal in calendars:
                        try:
                            if getattr(cal, "name", "").strip() == target_calendar_name:
                                return cal
                        except Exception:
                            pass
                        if str(cal.url).rstrip("/").endswith(
                            "/" + quote(target_calendar_name, safe="")
                        ):
                            return cal

                for cal in calendars:
                    try:
                        if "VEVENT" in cal.get_supported_components():
                            return cal
                    except Exception:
                        pass

                return calendars[0]
        except Exception as exc:
            discovery_error = exc

        raise HTTPException(
            status_code=502,
            detail=(
                "Synology CalDAV calendar discovery failed. "
                f"PROPFIND error: {propfind_error}; "
                f"Candidate error: {last_candidate_error}; "
                f"Principal error: {discovery_error}"
            ),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Synology CalDAV connection failed: {exc}",
        ) from exc


def parse_event_datetime(value: str) -> datetime:
    raw = value.strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Event datetime is required.")
    try:
        normalized = raw.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid datetime format: {value}",
        ) from exc


def format_ics_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        return value.strftime("%Y%m%dT%H%M%S")
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def format_ics_datetime_line(name: str, value: datetime) -> str:
    if value.tzinfo is None:
        return f"{name};TZID=Asia/Seoul:{format_ics_datetime(value)}"
    return f"{name}:{format_ics_datetime(value)}"


def format_ics_date_line(name: str, value: datetime) -> str:
    return f"{name};VALUE=DATE:{value.strftime('%Y%m%d')}"


def escape_ics_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def unfold_ics(data: str) -> str:
    return re.sub(r"\r?\n[ \t]", "", data or "")


def vevent_ics(data: str) -> str:
    match = re.search(r"BEGIN:VEVENT\r?\n(.*?)\r?\nEND:VEVENT", data or "", re.DOTALL)
    return match.group(1) if match else data


def ics_field(data: str, name: str) -> str:
    match = re.search(rf"^{name}(?:;[^:]*)?:(.*)$", data, re.MULTILINE)
    return match.group(1).strip() if match else ""


def unescape_ics_text(value: str) -> str:
    result = value or ""
    result = result.replace("\\n", "\n").replace("\\N", "\n")
    result = result.replace("\\,", ",").replace("\\;", ";")
    result = result.replace("\\\\", "\\")
    return result.strip()


SEOUL_TZ = ZoneInfo("Asia/Seoul")

def parse_ics_datetime(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""

    if ":" in raw:
        params, date_value = raw.split(":", 1)
    else:
        params, date_value = "", raw

    if "T" not in date_value:
        try:
            dt = datetime.strptime(date_value[:8], "%Y%m%d")
            return dt.date().isoformat()
        except ValueError:
            return ""

    if date_value.endswith("Z"):
        dt = datetime.strptime(date_value, "%Y%m%dT%H%M%SZ")
        return dt.replace(tzinfo=timezone.utc).isoformat()

    tz = SEOUL_TZ
    match = re.search(r"TZID=([^:;]+)", params)
    if match:
        try:
            tz = ZoneInfo(match.group(1))
        except Exception:
            tz = SEOUL_TZ

    try:
        dt = datetime.strptime(date_value[:15], "%Y%m%dT%H%M%S")
    except ValueError:
        return ""

    return dt.replace(tzinfo=tz).astimezone(timezone.utc).isoformat()


def is_ics_all_day(data: str) -> bool:
    return bool(re.search(r"^DTSTART(?:;[^:]*)?VALUE=DATE(?:;[^:]*)?:", data, re.MULTILINE))


def company_address_for_calendar(company_name: str) -> str:
    target = company_name.strip()
    if not target:
        return ""
    try:
        result = (
            supabase.table("companies")
            .select("address")
            .eq("company_name", target)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if rows:
            return (rows[0].get("address") or "").strip()
    except Exception:
        return ""
    return ""


def calendar_title_company_candidates(value: str) -> list[str]:
    target = value.strip()
    if not target:
        return []
    candidates = [target]
    completed_prefixes = ["완)", "완）"]
    for prefix in completed_prefixes:
        if target.startswith(prefix):
            cleaned = target[len(prefix):].strip()
            if cleaned and cleaned not in candidates:
                candidates.append(cleaned)
    return candidates


def normalize_calendar_company_match_key(value: str) -> str:
    key = normalize_company_key(value)
    key = re.sub(r"[\W_]+", "", key, flags=re.UNICODE)
    return key


def company_name_if_exists(value: str) -> str:
    candidates = calendar_title_company_candidates(value)
    title_keys = [
        normalize_calendar_company_match_key(candidate)
        for candidate in candidates
        if normalize_calendar_company_match_key(candidate)
    ]
    if not title_keys:
        return ""

    try:
        result = supabase.table("companies").select("company_name").execute()
    except Exception:
        return ""

    matches: list[str] = []
    for row in result.data or []:
        company_name = (row.get("company_name") or "").strip()
        company_key = normalize_calendar_company_match_key(company_name)
        if not company_name or not company_key:
            continue
        if any(
            company_key == title_key or company_key in title_key
            for title_key in title_keys
        ):
            matches.append(company_name)

    if not matches:
        return ""

    matches.sort(
        key=lambda name: len(normalize_calendar_company_match_key(name)),
        reverse=True,
    )
    return matches[0]


def ics_fields(data: str, name: str) -> list[str]:
    return [
        match.group(1).strip()
        for match in re.finditer(rf"^{name}(?:;[^:]*)?:(.*)$", data, re.MULTILINE)
    ]


EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)


def inspector_display_name(inspector: dict | CalendarInspector) -> str:
    if isinstance(inspector, CalendarInspector):
        name = inspector.name.strip()
        email = inspector.email.strip()
        phone = inspector.phone.strip()
    else:
        name = str(inspector.get("name") or "").strip()
        email = str(inspector.get("email") or "").strip()
        phone = str(inspector.get("phone") or "").strip()
    if name and email:
        return f"{name} <{email}>"
    if name and phone:
        return f"{name} {phone}"
    return name or email or phone


def parse_inspector_text(value: str) -> dict:
    raw = value.strip()
    if not raw:
        return {"name": "", "email": "", "phone": ""}
    email_match = EMAIL_RE.search(raw)
    email = email_match.group(0).strip() if email_match else ""
    name = EMAIL_RE.sub("", raw).replace("<>", "").replace("<", "").replace(">", "").strip(" ,/")
    if not name and email:
        name = email
    return {"name": name, "email": email, "phone": ""}


def parse_description_inspectors(memo: str) -> list[dict]:
    inspectors: list[dict] = []
    for line in memo.splitlines():
        stripped = line.strip()
        if not stripped.startswith("점검자:"):
            continue
        parsed = parse_inspector_text(stripped.split(":", 1)[1])
        if parsed["name"] or parsed["email"]:
            inspectors.append(parsed)
    return inspectors


def parse_attendees(data: str) -> list[dict]:
    inspectors: list[dict] = []
    for line in re.finditer(r"^ATTENDEE((?:;[^:]*)?):(.*)$", data, re.MULTILINE):
        params = line.group(1)
        address = re.sub(
            r"^mailto:",
            "",
            unescape_ics_text(line.group(2).strip()),
            flags=re.IGNORECASE,
        ).strip()
        cn_match = re.search(r";CN=(?:\"([^\"]+)\"|([^;:]+))", params, re.IGNORECASE)
        name = (
            unescape_ics_text(cn_match.group(1) or cn_match.group(2)).strip()
            if cn_match
            else ""
        )
        email = address if EMAIL_RE.fullmatch(address) else ""
        if not email:
            continue
        name = name or contact_name_for_email(email) or email
        inspectors.append({"name": name, "email": email, "phone": ""})
    return inspectors


def enrich_inspector_names(inspectors: list[dict]) -> list[dict]:
    enriched: list[dict] = []
    for inspector in inspectors:
        if not isinstance(inspector, dict):
            continue
        name = str(inspector.get("name") or "").strip()
        email = str(inspector.get("email") or "").strip()
        phone = str(inspector.get("phone") or "").strip()
        if email and (not name or name.lower() == email.lower()):
            name = contact_name_for_email(email) or name or email
        enriched.append({"name": name, "email": email, "phone": phone})
    return enriched


def unique_inspectors(inspectors: list[dict]) -> list[dict]:
    by_email: dict[str, dict] = {}
    no_email: list[dict] = []
    seen_names: set[str] = set()
    result: list[dict] = []
    for inspector in inspectors:
        name = str(inspector.get("name") or "").strip()
        email = str(inspector.get("email") or "").strip()
        phone = str(inspector.get("phone") or "").strip()
        if not name and not email and not phone:
            continue
        if email:
            key = email.lower()
            existing = by_email.get(key)
            if existing is None:
                by_email[key] = {"name": name, "email": email, "phone": phone}
                continue
            existing_name = str(existing.get("name") or "").strip()
            if (
                name
                and (not existing_name or existing_name.lower() == key)
                and name.lower() != key
            ):
                existing["name"] = name
            if phone and not str(existing.get("phone") or "").strip():
                existing["phone"] = phone
            continue
        name_key = name.lower()
        if name_key in seen_names:
            continue
        seen_names.add(name_key)
        no_email.append({"name": name, "email": email, "phone": phone})
    result.extend(by_email.values())
    result.extend(no_email)
    return enrich_inspector_names(result)


def calendar_event_inspectors(event: CalendarEventCreate) -> list[dict]:
    inspectors = [
        {
            "name": inspector.name.strip(),
            "email": inspector.email.strip(),
            "phone": inspector.phone.strip(),
        }
        for inspector in event.inspectors
    ]
    # Backward compatibility only: a legacy inspector string can populate the
    # app's internal inspector list, but plain names are never CalDAV guests.
    if not inspectors and event.inspector.strip():
        for part in re.split(r"[,\n]+", event.inspector):
            parsed = parse_inspector_text(part)
            if parsed["name"] or parsed["email"]:
                inspectors.append(parsed)
    return unique_inspectors(inspectors)


def attendee_ics_lines(inspectors: list[dict]) -> list[str]:
    lines: list[str] = []
    for inspector in inspectors:
        email = str(inspector.get("email") or "").strip()
        if not EMAIL_RE.fullmatch(email):
            continue
        name = str(inspector.get("name") or "").strip() or email
        lines.append(
            f"ATTENDEE;CN={escape_ics_text(name)};ROLE=REQ-PARTICIPANT:mailto:{escape_ics_text(email)}"
        )
    return lines


def calendar_event_object(uid: str, calendar_scope: str = "company_shared"):
    calendar = calendar_entry_for_scope(calendar_scope)["calendar"]
    try:
        return calendar.event_by_uid(uid)
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Synology CalDAV event not found: {exc}",
        ) from exc


def caldav_event_attr(event, name: str) -> str:
    try:
        value = getattr(event, name, "")
        if callable(value):
            value = value()
        return str(value or "")
    except Exception:
        return ""


def calendar_event_to_json(event, calendar_meta: dict | None = None) -> dict:
    data = vevent_ics(unfold_ics(getattr(event, "data", "") or ""))
    memo = unescape_ics_text(ics_field(data, "DESCRIPTION"))
    company_name = ""
    for line in memo.splitlines():
        if line.strip().startswith("업체명:"):
            company_name = line.split(":", 1)[1].strip()
            break
    title = unescape_ics_text(ics_field(data, "SUMMARY"))
    if not company_name:
        company_name = company_name_if_exists(title)

    inspectors = unique_inspectors(
        parse_description_inspectors(memo) + parse_attendees(data)
    )
    inspector = ", ".join(inspector_display_name(item) for item in inspectors)

    meta = calendar_meta or {}
    return {
        "uid": ics_field(data, "UID"),
        "company_name": company_name,
        "title": title,
        "calendar_scope": meta.get("scope", "company_shared"),
        "calendarScope": meta.get("scope", "company_shared"),
        "calendar_name": meta.get("name", ""),
        "calendarName": meta.get("name", ""),
        "calendar_url": meta.get("url", ""),
        "calendarUrl": meta.get("url", ""),
        "href": caldav_event_attr(event, "url"),
        "etag": caldav_event_attr(event, "etag"),
        "can_edit": bool(meta.get("can_write", True)),
        "memo": memo,
        "location": unescape_ics_text(ics_field(data, "LOCATION")),
        "inspector": inspector,
        "inspectors": inspectors,
        "all_day": is_ics_all_day(data),
        "start_at": parse_ics_datetime(ics_field(data, "DTSTART")),
        "end_at": parse_ics_datetime(ics_field(data, "DTEND")),
    }


def create_synology_calendar_event(
    event: CalendarEventCreate,
    uid_override: str | None = None,
    calendar_scope: str | None = None,
) -> dict:
    scope = normalize_calendar_scope(calendar_scope or event.calendar_scope or "company_shared")
    entry = calendar_entry_for_scope(scope)
    if not entry["can_write"]:
        raise HTTPException(
            status_code=403,
            detail=f"Calendar '{entry['name']}' is read-only for this account.",
        )
    calendar = entry["calendar"]
    start_at = parse_event_datetime(event.start_at)
    end_at = parse_event_datetime(event.end_at)
    if event.all_day:
        start_at = datetime(start_at.year, start_at.month, start_at.day)
        end_at = datetime(end_at.year, end_at.month, end_at.day)
        if end_at <= start_at:
            end_at = start_at + timedelta(days=1)
    elif end_at <= start_at:
        raise HTTPException(status_code=400, detail="end_at must be after start_at.")

    uid = uid_override or f"{uuid.uuid4()}@hsinfra"
    title = event.title.strip() or event.company_name.strip() or "일정"
    description = event.memo.strip()
    company_name = event.company_name.strip()
    inspectors = calendar_event_inspectors(event)
    inspector_lines = [
        f"점검자: {inspector_display_name(inspector)}"
        for inspector in inspectors
        if inspector_display_name(inspector)
    ]
    metadata_lines = []
    if company_name:
        metadata_lines.append(f"업체명: {company_name}")
    metadata_lines.extend(inspector_lines)
    description = "\n".join([*metadata_lines, description]).strip()
    location = event.location.strip() or company_address_for_calendar(company_name)
    attendee_lines = attendee_ics_lines(inspectors)

    ics = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//HS Infra Inspection App//Synology Calendar//KO",
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
            (
                format_ics_date_line("DTSTART", start_at)
                if event.all_day
                else format_ics_datetime_line("DTSTART", start_at)
            ),
            (
                format_ics_date_line("DTEND", end_at)
                if event.all_day
                else format_ics_datetime_line("DTEND", end_at)
            ),
            f"SUMMARY:{escape_ics_text(title)}",
            f"DESCRIPTION:{escape_ics_text(description)}",
            *([f"LOCATION:{escape_ics_text(location)}"] if location else []),
            *attendee_lines,
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )

    try:
        saved = calendar.save_event(ics)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Synology CalDAV event create failed: {exc}",
        ) from exc

    return {
        "created": True,
        "uid": uid,
        "title": title,
        "start_at": start_at.isoformat(),
        "end_at": end_at.isoformat(),
        "location": location,
        "inspector": ", ".join(inspector_display_name(item) for item in inspectors),
        "inspectors": inspectors,
        "all_day": event.all_day,
        "calendar_scope": entry["scope"],
        "calendarScope": entry["scope"],
        "calendar_name": entry["name"],
        "calendarName": entry["name"],
        "calendar_url": entry["url"],
        "calendarUrl": entry["url"],
        "href": str(getattr(saved, "url", "")),
        "etag": "",
        "can_edit": entry["can_write"],
        "canEdit": entry["can_write"],
        "url": str(getattr(saved, "url", "")),
    }


def update_synology_calendar_event(uid: str, event: CalendarEventCreate) -> dict:
    entry = calendar_entry_for_scope(event.calendar_scope)
    if not entry["can_write"]:
        raise HTTPException(
            status_code=403,
            detail=f"Calendar '{entry['name']}' is read-only for this account.",
        )
    existing = calendar_event_object(uid, event.calendar_scope)
    try:
        existing.delete()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Synology CalDAV event delete before update failed: {exc}",
        ) from exc
    return create_synology_calendar_event(event, uid_override=uid, calendar_scope=event.calendar_scope)


def delete_synology_calendar_event(uid: str, calendar_scope: str = "company_shared") -> dict:
    entry = calendar_entry_for_scope(calendar_scope)
    if not entry["can_write"]:
        raise HTTPException(
            status_code=403,
            detail=f"Calendar '{entry['name']}' is read-only for this account.",
        )
    existing = calendar_event_object(uid, calendar_scope)
    try:
        existing.delete()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Synology CalDAV event delete failed: {exc}",
        ) from exc
    return {"deleted": True, "uid": uid}


def enrich_calendar_events_with_inspections(events: list[dict]) -> list[dict]:
    uids = [item.get("uid") for item in events if item.get("uid")]
    hrefs = [item.get("href") for item in events if item.get("href")]
    linked_by_uid: dict[str, dict] = {}
    linked_by_href: dict[str, dict] = {}
    try:
        query = supabase.table("inspections").select(
            "id, calendar_event_uid, calendar_scope, calendar_url, calendar_href"
        )
        if uids:
            rows = query.in_("calendar_event_uid", uids).execute().data or []
            linked_by_uid = {
                str(row.get("calendar_event_uid") or ""): row
                for row in rows
                if row.get("calendar_event_uid")
            }
        if hrefs:
            rows = (
                supabase.table("inspections")
                .select("id, calendar_event_uid, calendar_scope, calendar_url, calendar_href")
                .in_("calendar_href", hrefs)
                .execute()
                .data
                or []
            )
            linked_by_href = {
                str(row.get("calendar_href") or ""): row
                for row in rows
                if row.get("calendar_href")
            }
    except Exception:
        linked_by_uid = {}
        linked_by_href = {}

    for item in events:
        linked = linked_by_uid.get(item.get("uid", "")) or linked_by_href.get(item.get("href", ""))
        item["inspection_id"] = str((linked or {}).get("id") or "")
        item["inspectionLinked"] = bool(linked)
        item["inspection_linked"] = bool(linked)
    return events


def calendar_event_payload_from_json(event: dict, synced_at: str) -> dict:
    inspectors = event.get("inspectors") or []
    if not isinstance(inspectors, list):
        inspectors = []
    inspectors = unique_inspectors(inspectors)
    attendees = [item for item in inspectors if isinstance(item, dict) and str(item.get("email") or "").strip()]
    payload = {
        "uid": str(event.get("uid") or "").strip(),
        "href": str(event.get("href") or "").strip(),
        "etag": str(event.get("etag") or "").strip(),
        "calendar_scope": normalize_calendar_scope(str(event.get("calendar_scope") or event.get("calendarScope") or "company_shared")),
        "calendar_url": str(event.get("calendar_url") or event.get("calendarUrl") or "").strip(),
        "calendar_name": str(event.get("calendar_name") or event.get("calendarName") or "").strip(),
        "company_name": str(event.get("company_name") or event.get("companyName") or "").strip(),
        "title": str(event.get("title") or "").strip(),
        "description": str(event.get("memo") or event.get("description") or "").strip(),
        "start_at": event.get("start_at") or event.get("startAt"),
        "end_at": event.get("end_at") or event.get("endAt"),
        "location": str(event.get("location") or "").strip(),
        "inspector": str(event.get("inspector") or "").strip(),
        "inspectors": inspectors,
        "attendees": attendees,
        "can_edit": bool(event.get("can_edit", event.get("canEdit", True))),
        "all_day": bool(event.get("all_day", event.get("allDay", False))),
        "sync_status": "synced",
        "last_synced_at": synced_at,
        "deleted": False,
    }
    linked = enrich_calendar_events_with_inspections([event])[0]
    inspection_id = str(linked.get("inspection_id") or linked.get("inspectionId") or "").strip()
    if inspection_id:
        payload["inspection_id"] = inspection_id
    allowed = calendar_event_column_names()
    return {key: value for key, value in payload.items() if key in allowed}


def find_calendar_event_row(calendar_url: str, uid: str, href: str) -> dict | None:
    table = supabase.table("calendar_events")
    if calendar_url and uid:
        rows = table.select("*").eq("calendar_url", calendar_url).eq("uid", uid).limit(1).execute().data or []
        if rows:
            return rows[0]
    if href:
        rows = supabase.table("calendar_events").select("*").eq("href", href).limit(1).execute().data or []
        if rows:
            return rows[0]
    return None


def upsert_calendar_event_row(payload: dict) -> tuple[str, dict]:
    existing = find_calendar_event_row(
        str(payload.get("calendar_url") or ""),
        str(payload.get("uid") or ""),
        str(payload.get("href") or ""),
    )
    if existing:
        result = (
            supabase.table("calendar_events")
            .update(payload)
            .eq("id", existing["id"])
            .execute()
        )
        return "updated", (result.data or [payload])[0]
    result = supabase.table("calendar_events").insert(payload).execute()
    return "inserted", (result.data or [payload])[0]


def upsert_calendar_event_for_inspection(
    inspection_id: str,
    event_result: dict,
    event_payload: CalendarEventCreate,
) -> None:
    try:
        event = {
            "uid": event_result.get("uid", ""),
            "href": event_result.get("href", event_result.get("url", "")),
            "etag": event_result.get("etag", ""),
            "calendar_scope": event_result.get("calendar_scope", event_payload.calendar_scope),
            "calendarScope": event_result.get("calendar_scope", event_payload.calendar_scope),
            "calendar_name": event_result.get("calendar_name", event_result.get("calendarName", "")),
            "calendarName": event_result.get("calendar_name", event_result.get("calendarName", "")),
            "calendar_url": event_result.get("calendar_url", event_result.get("calendarUrl", "")),
            "calendarUrl": event_result.get("calendar_url", event_result.get("calendarUrl", "")),
            "company_name": event_payload.company_name,
            "companyName": event_payload.company_name,
            "title": event_payload.title,
            "memo": event_payload.memo,
            "description": event_payload.memo,
            "start_at": event_result.get("start_at", event_payload.start_at),
            "startAt": event_result.get("start_at", event_payload.start_at),
            "end_at": event_result.get("end_at", event_payload.end_at),
            "endAt": event_result.get("end_at", event_payload.end_at),
            "location": event_result.get("location", event_payload.location),
            "inspector": event_result.get("inspector", event_payload.inspector),
            "inspectors": event_result.get("inspectors", event_payload.inspectors),
            "all_day": event_result.get("all_day", event_payload.all_day),
            "allDay": event_result.get("all_day", event_payload.all_day),
            "can_edit": event_result.get("can_edit", event_result.get("canEdit", True)),
        }
        payload = calendar_event_payload_from_json(
            event,
            datetime.now(timezone.utc).isoformat(),
        )
        payload["inspection_id"] = inspection_id
        payload["deleted"] = False
        payload["sync_status"] = "synced"
        upsert_calendar_event_row(payload)
    except Exception as exc:
        print(f"calendar_events inspection upsert skipped: {exc}")


def mark_calendar_event_deleted_for_inspection(row: dict) -> None:
    try:
        now = datetime.now(timezone.utc).isoformat()
        inspection_id = str(row.get("id") or "").strip()
        uid = str(row.get("calendar_event_uid") or "").strip()
        href = str(row.get("calendar_href") or "").strip()
        update = {"deleted": True, "sync_status": "deleted", "last_synced_at": now}
        if inspection_id:
            supabase.table("calendar_events").update(update).eq("inspection_id", inspection_id).execute()
        if uid:
            supabase.table("calendar_events").update(update).eq("uid", uid).execute()
        if href:
            supabase.table("calendar_events").update(update).eq("href", href).execute()
    except Exception as exc:
        print(f"calendar_events inspection delete mark skipped: {exc}")


def calendar_event_json_from_row(row: dict) -> dict:
    inspectors = row.get("inspectors") or []
    if isinstance(inspectors, str):
        try:
            inspectors = json.loads(inspectors)
        except Exception:
            inspectors = []
    attendees = row.get("attendees") or []
    if isinstance(attendees, str):
        try:
            attendees = json.loads(attendees)
        except Exception:
            attendees = []
    inspectors = unique_inspectors(inspectors) if isinstance(inspectors, list) else []
    attendees = unique_inspectors(attendees) if isinstance(attendees, list) else []
    return {
        "uid": str(row.get("uid") or ""),
        "company_name": str(row.get("company_name") or ""),
        "companyName": str(row.get("company_name") or ""),
        "title": str(row.get("title") or ""),
        "memo": str(row.get("description") or ""),
        "description": str(row.get("description") or ""),
        "start_at": row.get("start_at"),
        "startAt": row.get("start_at"),
        "end_at": row.get("end_at"),
        "endAt": row.get("end_at"),
        "location": str(row.get("location") or ""),
        "inspector": str(row.get("inspector") or ""),
        "inspectors": inspectors,
        "attendees": attendees,
        "all_day": bool(row.get("all_day")),
        "allDay": bool(row.get("all_day")),
        "calendar_scope": normalize_calendar_scope(str(row.get("calendar_scope") or "company_shared")),
        "calendarScope": normalize_calendar_scope(str(row.get("calendar_scope") or "company_shared")),
        "calendar_name": str(row.get("calendar_name") or ""),
        "calendarName": str(row.get("calendar_name") or ""),
        "calendar_url": str(row.get("calendar_url") or ""),
        "calendarUrl": str(row.get("calendar_url") or ""),
        "href": str(row.get("href") or ""),
        "etag": str(row.get("etag") or ""),
        "can_edit": bool(row.get("can_edit", True)),
        "canEdit": bool(row.get("can_edit", True)),
        "inspection_id": str(row.get("inspection_id") or ""),
        "inspectionId": str(row.get("inspection_id") or ""),
        "inspection_linked": bool(row.get("inspection_id")),
        "inspectionLinked": bool(row.get("inspection_id")),
        "sync_status": str(row.get("sync_status") or "synced"),
        "syncStatus": str(row.get("sync_status") or "synced"),
        "last_synced_at": row.get("last_synced_at"),
        "lastSyncedAt": row.get("last_synced_at"),
        "deleted": bool(row.get("deleted")),
    }


def parse_requested_calendar_scopes(scopes: str) -> set[str]:
    requested = {
        normalize_calendar_scope(item)
        for item in scopes.split(",")
        if normalize_calendar_scope(item) in {"personal", "company_shared", "other"}
    }
    return requested or {"personal", "company_shared"}


def calendar_event_dedupe_keys(item: dict) -> list[str]:
    keys = []
    inspection_id = str(item.get("inspection_id") or item.get("inspectionId") or "").strip()
    uid = str(item.get("uid") or "").strip()
    href = str(item.get("href") or "").strip()
    calendar_url = str(item.get("calendar_url") or item.get("calendarUrl") or "").strip()
    if inspection_id:
        keys.append(f"inspection:{inspection_id}")
    if uid:
        keys.append(f"uid:{uid}")
    if href:
        keys.append(f"href:{href}")
    if calendar_url and uid:
        keys.append(f"calendar_uid:{calendar_url}|{uid}")
    if calendar_url and href:
        keys.append(f"calendar_href:{calendar_url}|{href}")
    return keys or [
        "fallback:"
        + "|".join(
            [
                str(item.get("title") or ""),
                str(item.get("start_at") or item.get("startAt") or ""),
                str(item.get("end_at") or item.get("endAt") or ""),
            ]
        )
    ]


def dedupe_calendar_event_items(items: list[dict]) -> list[dict]:
    canonical: dict[str, dict] = {}
    aliases: dict[str, str] = {}
    for item in items:
        keys = calendar_event_dedupe_keys(item)
        existing_key = next((aliases[key] for key in keys if key in aliases), None)
        if existing_key is None:
            existing_key = keys[0]
            canonical[existing_key] = item
        else:
            existing = canonical[existing_key]
            if not existing.get("inspection_id") and item.get("inspection_id"):
                canonical[existing_key] = item
            elif existing.get("deleted") and not item.get("deleted"):
                canonical[existing_key] = item
        for key in keys:
            aliases[key] = existing_key
    return list(canonical.values())


def list_calendar_events_from_db(start: str, end: str, scopes: str = "personal,company_shared") -> list[dict]:
    start_at = parse_event_datetime(start)
    end_at = parse_event_datetime(end)
    if end_at <= start_at:
        raise HTTPException(status_code=400, detail="end must be after start.")
    rows = (
        supabase.table("calendar_events")
        .select("*")
        .lt("start_at", end_at.isoformat())
        .gt("end_at", start_at.isoformat())
        .eq("deleted", False)
        .execute()
        .data
        or []
    )
    requested = parse_requested_calendar_scopes(scopes)
    result = [
        calendar_event_json_from_row(row)
        for row in rows
        if normalize_calendar_scope(str(row.get("calendar_scope") or "")) in requested
    ]
    result = dedupe_calendar_event_items(result)
    result.sort(key=lambda item: item.get("start_at") or "")
    return result


def sync_calendar_events_to_db(start: str, end: str, scopes: str = "personal,company_shared") -> dict:
    run_started_at = datetime.now(timezone.utc).isoformat()
    try:
        start_at = parse_event_datetime(start)
        end_at = parse_event_datetime(end)
        if end_at <= start_at:
            raise HTTPException(status_code=400, detail="end must be after start.")
        requested = parse_requested_calendar_scopes(scopes)
        synced_at = datetime.now(timezone.utc).isoformat()
        inserted = 0
        updated = 0
        seen_keys: set[tuple[str, str, str]] = set()
        synced_events: list[dict] = []
        discovered_entries = synology_calendar_entries()
        discovered_calendars = [calendar_entry_debug(entry) for entry in discovered_entries]
        processed_calendars: list[dict] = []
        log_calendar_debug(
            "sync_start",
            {
                "requested_scopes": sorted(requested),
                "start_at": start_at.isoformat(),
                "end_at": end_at.isoformat(),
                "discovered_calendars": discovered_calendars,
            },
        )

        for entry in discovered_entries:
            if entry["scope"] not in requested:
                continue
            selected_calendar = calendar_entry_debug(entry)
            try:
                events = entry["calendar"].date_search(start=start_at, end=end_at)
            except Exception as exc:
                log_calendar_debug(
                    "sync_calendar_query_failed",
                    {**selected_calendar, "error": str(exc)},
                )
                raise HTTPException(
                    status_code=502,
                    detail=f"Synology CalDAV event query failed ({entry['name']}): {exc}",
                ) from exc
            selected_calendar["event_count"] = len(events)
            processed_calendars.append(selected_calendar)
            log_calendar_debug("sync_calendar_selected", selected_calendar)
            for raw_event in events:
                event = calendar_event_to_json(raw_event, entry)
                payload = calendar_event_payload_from_json(event, synced_at)
                action, row = upsert_calendar_event_row(payload)
                if action == "inserted":
                    inserted += 1
                else:
                    updated += 1
                key = (
                    str(payload.get("calendar_url") or ""),
                    str(payload.get("uid") or ""),
                    str(payload.get("href") or ""),
                )
                seen_keys.add(key)
                synced_events.append(calendar_event_json_from_row(row))

        existing_rows = (
            supabase.table("calendar_events")
            .select("id, uid, href, calendar_url, calendar_scope, start_at, end_at, deleted")
            .lt("start_at", end_at.isoformat())
            .gt("end_at", start_at.isoformat())
            .eq("deleted", False)
            .execute()
            .data
            or []
        )
        deleted = 0
        for row in existing_rows:
            scope = normalize_calendar_scope(str(row.get("calendar_scope") or ""))
            if scope not in requested:
                continue
            key = (
                str(row.get("calendar_url") or ""),
                str(row.get("uid") or ""),
                str(row.get("href") or ""),
            )
            if key in seen_keys:
                continue
            supabase.table("calendar_events").update(
                {"deleted": True, "sync_status": "deleted", "last_synced_at": synced_at}
            ).eq("id", row["id"]).execute()
            deleted += 1

        result = {
            "success": True,
            "inserted": inserted,
            "updated": updated,
            "deleted": deleted,
            "synced": inserted + updated,
            "last_synced_at": synced_at,
            "lastSyncedAt": synced_at,
            "scopes": sorted(requested),
            "events": sorted(synced_events, key=lambda item: item.get("start_at") or ""),
            "discovered_calendars": discovered_calendars,
            "processed_calendars": processed_calendars,
        }
        log_calendar_debug(
            "sync_result",
            {
                "inserted": inserted,
                "updated": updated,
                "deleted": deleted,
                "processed_calendars": processed_calendars,
            },
        )
        record_calendar_sync_run(
            status="success",
            started_at=run_started_at,
            finished_at=synced_at,
            scopes=",".join(sorted(requested)),
            start_at=start_at.isoformat(),
            end_at=end_at.isoformat(),
            inserted=inserted,
            updated=updated,
            deleted=deleted,
        )
        return result
    except Exception as exc:
        finished_at = datetime.now(timezone.utc).isoformat()
        detail = getattr(exc, "detail", str(exc))
        record_calendar_sync_run(
            status="failed",
            started_at=run_started_at,
            finished_at=finished_at,
            scopes=scopes,
            start_at=start,
            end_at=end,
            error_message=str(detail),
        )
        raise


def run_calendar_sync_with_lock(start: str, end: str, scopes: str = "personal,company_shared") -> dict:
    if not calendar_sync_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail={
                "status": "already_running",
                "message": "Calendar sync is already running.",
            },
        )
    try:
        return sync_calendar_events_to_db(start, end, scopes)
    finally:
        calendar_sync_lock.release()


def list_synology_calendar_events(start: str, end: str, scopes: str = "personal,company_shared") -> list[dict]:
    start_at = parse_event_datetime(start)
    end_at = parse_event_datetime(end)
    if end_at <= start_at:
        raise HTTPException(status_code=400, detail="end must be after start.")

    requested_scopes = {
        normalize_calendar_scope(item)
        for item in scopes.split(",")
        if normalize_calendar_scope(item) in {"personal", "company_shared", "other"}
    }
    if not requested_scopes:
        requested_scopes = {"personal", "company_shared"}

    results: list[dict] = []
    for entry in synology_calendar_entries():
        if entry["scope"] not in requested_scopes:
            continue
        try:
            events = entry["calendar"].date_search(start=start_at, end=end_at)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Synology CalDAV event query failed ({entry['name']}): {exc}",
            ) from exc
        results.extend(calendar_event_to_json(event, entry) for event in events)

    results = enrich_calendar_events_with_inspections(results)
    results.sort(key=lambda item: item.get("start_at", ""))
    return results


def schedule_payload_from_create(schedule: InspectionScheduleCreate) -> dict:
    company_name = schedule.company_name.strip()
    if not company_name:
        raise HTTPException(status_code=400, detail="company_name is required.")
    company_id = ensure_company(company_name)
    return {
        "company_id": company_id,
        "date": schedule.date,
        "category": schedule.category,
        "time": schedule.time,
    }


@app.get("/")
@app.get("/api")
@app.get("/api/")
def home():
    return {"message": "web12 backend running"}


@app.get("/companies")
@app.get("/api/companies")
def get_companies():
    result = (
        supabase.table("companies")
        .select("*")
        .order("company_name", desc=False)
        .execute()
    )
    return result.data


@app.get("/nas/check")
@app.get("/api/nas/check")
def check_nas_connection():
    base_url, username, _, root_path = get_filestation_config()
    sid_base_url, sid = filestation_login()
    filestation_logout(sid_base_url, sid)
    return {
        "success": True,
        "base_url": base_url,
        "username": username,
        "root_path": root_path,
        "filestation_login": True,
    }


@app.post("/companies")
@app.post("/api/companies")
def create_company(company: CompanyCreate):
    payload = {
        key: value
        for key, value in company.dict().items()
        if key in company_column_names()
    }
    result = supabase.table("companies").insert(payload).execute()
    return result.data[0]


@app.put("/companies/{company_id}")
@app.put("/api/companies/{company_id}")
def update_company(company_id: str, company: CompanyCreate):
    try:
        payload = {
            key: value
            for key, value in company.dict().items()
            if key in company_column_names()
        }
        result = (
            supabase.table("companies")
            .update(payload)
            .eq("id", company_id)
            .execute()
        )
        if not result.data:
            raise HTTPException(status_code=404, detail="Company not found.")
        return result.data[0]
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Company update failed: {exc}",
        ) from exc


@app.delete("/companies/{company_id}")
@app.delete("/api/companies/{company_id}")
def delete_company(company_id: str):
    try:
        existing = (
            supabase.table("companies")
            .select("id")
            .eq("id", company_id)
            .limit(1)
            .execute()
        )
        if not existing.data:
            raise HTTPException(status_code=404, detail="Company not found.")

        supabase.table("companies").delete().eq("id", company_id).execute()
        return {"deleted": True, "id": company_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Company delete failed: {exc}",
        ) from exc

@app.post("/companies/sync-from-spreadsheet")
@app.post("/api/companies/sync-from-spreadsheet")
def sync_companies_from_spreadsheet():
    xlsx_bytes = export_spreadsheet_xlsx()
    companies = company_rows_from_spreadsheet(xlsx_bytes)
    if not companies:
        raise HTTPException(
            status_code=400,
            detail="No company rows found in spreadsheet.",
        )
    result = upsert_companies(companies)
    return result



@app.get("/inspections")
@app.get("/api/inspections")
def get_inspections():
    inspections = (
        supabase.table("inspections")
        .select("*")
        .order("date", desc=True)
        .execute()
    )
    rows = inspections.data or []
    company_ids = list({row.get("company_id") for row in rows if row.get("company_id")})
    inspection_ids = list({row.get("id") for row in rows if row.get("id")})

    companies_by_id = {}
    if company_ids:
        companies = (
            supabase.table("companies")
            .select("id, company_name")
            .in_("id", company_ids)
            .execute()
        )
        companies_by_id = {company["id"]: company for company in companies.data or []}

    photos_by_inspection_id = {}
    if inspection_ids:
        photos = select_inspection_photos(
            lambda columns: supabase.table("inspection_photos")
            .select(columns)
            .in_("inspection_id", inspection_ids)
        )
        for photo in photos:
            photos_by_inspection_id.setdefault(photo.get("inspection_id"), []).append(photo)

    for row in rows:
        company = companies_by_id.get(row.get("company_id"), {})
        row["companies"] = company
        row["company_name"] = company.get("company_name", "")
        row["inspection_photos"] = photos_by_inspection_id.get(row.get("id"), [])

    return rows


@app.post("/inspections")
@app.post("/api/inspections")
def create_inspection_record(inspection: InspectionCreate):
    payload = inspection_payload_from_create(inspection)
    allowed_columns = inspection_column_names()
    if "revision" in allowed_columns:
        payload["revision"] = 1
    if "updated_at" in allowed_columns:
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = supabase.table("inspections").insert(payload).execute()
    return result.data[0]


@app.put("/inspections/{inspection_id}")
@app.put("/api/inspections/{inspection_id}")
def update_inspection_record(inspection_id: str, inspection: InspectionCreate):
    existing = (
        supabase.table("inspections")
        .select("*")
        .eq("id", inspection_id)
        .limit(1)
        .execute()
    )
    if not existing.data:
        raise HTTPException(status_code=404, detail="Inspection not found.")

    existing_row = existing.data[0]
    existing_company_id = existing_row.get("company_id")
    old_company_name = ""
    if existing_company_id:
        existing_company = (
            supabase.table("companies")
            .select("company_name")
            .eq("id", existing_company_id)
            .limit(1)
            .execute()
        )
        if existing_company.data:
            old_company_name = existing_company.data[0].get("company_name", "")
    old_date = existing_row.get("date", "")
    old_category = existing_row.get("category", "")

    allowed_columns = inspection_column_names()
    current_revision = int(existing_row.get("revision") or 0)
    if "revision" in allowed_columns and inspection.revision > 0:
        if inspection.revision != current_revision:
            record_revision_conflict(inspection_id, inspection.revision, current_revision)
            raise HTTPException(status_code=409, detail="다른 사용자가 수정했습니다.")

    payload = inspection_payload_from_create(inspection)
    if "revision" in allowed_columns:
        payload["revision"] = current_revision + 1
    if "updated_at" in allowed_columns:
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = (
        supabase.table("inspections")
        .update(payload)
        .eq("id", inspection_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Inspection not found.")

    try:
        sync_inspection_nas_folder(
            inspection_id=inspection_id,
            old_company_name=old_company_name,
            old_date=old_date,
            old_category=old_category,
            new_company_name=inspection.company_name.strip(),
            new_date=inspection.date,
            new_category=inspection.category,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"NAS folder rename failed: {exc}",
        ) from exc

    return result.data[0]


@app.delete("/inspections/{inspection_id}")
@app.delete("/api/inspections/{inspection_id}")
def delete_inspection_record(inspection_id: str):
    existing = (
        supabase.table("inspections")
        .select("*")
        .eq("id", inspection_id)
        .limit(1)
        .execute()
    )
    supabase.table("inspection_photos").delete().eq(
        "inspection_id", inspection_id
    ).execute()
    supabase.table("inspections").delete().eq("id", inspection_id).execute()
    return {
        "deleted": True,
        "id": inspection_id,
        "calendar_deleted": False,
        "calendar_delete_error": CALENDAR_READ_ONLY_DETAIL,
    }


@app.get("/contacts/search")
@app.get("/api/contacts/search")
def search_contacts(q: str = "", limit: int = 30):
    return search_carddav_contacts(q, max(1, min(limit, 100)))


@app.get("/calendar/check")
@app.get("/api/calendar/check")
def check_calendar_connection():
    cal = synology_calendar()
    configured_url, _, _ = caldav_config()
    return {
        "success": True,
        "message": "Synology Calendar connection successful.",
        "configured_url": configured_url,
        "calendar_name": calendar_display_name(cal),
        "calendar_url": str(getattr(cal, "url", "")),
    }


@app.get("/calendar/calendars")
@app.get("/api/calendar/calendars")
def get_calendar_list():
    return public_calendar_entries()


@app.get("/calendar/events")
@app.get("/api/calendar/events")
def get_calendar_events(start: str, end: str, scopes: str = "personal,company_shared"):
    return list_calendar_events_from_db(start, end, scopes)


@app.post("/calendar/sync")
@app.post("/api/calendar/sync")
def sync_calendar_events(start: str, end: str, scopes: str = "personal,company_shared"):
    return run_calendar_sync_with_lock(start, end, scopes)


@app.get("/calendar/status")
@app.get("/api/calendar/status")
def get_calendar_status():
    events = table_rows(
        "calendar_events",
        "id, sync_status, deleted, inspection_id, last_synced_at",
    )
    total = len(events)
    pending = sum(1 for row in events if row.get("sync_status") == "pending")
    failed = sum(1 for row in events if row.get("sync_status") == "failed")
    deleted = sum(1 for row in events if row.get("deleted") is True or row.get("sync_status") == "deleted")
    unlinked = sum(1 for row in events if not row.get("deleted") and not row.get("inspection_id"))
    latest_event_sync = max(
        [str(row.get("last_synced_at") or "") for row in events if row.get("last_synced_at")],
        default="",
    )
    sync_summary = latest_calendar_sync_summary()
    last_run = sync_summary.get("last_run") or {}
    status = (
        "ok"
        if last_run.get("status") == "success"
        else "error"
        if sync_summary.get("last_error_message")
        else "ok"
    )
    return {
        "status": status,
        "last_synced_at": latest_event_sync or sync_summary.get("last_success_at"),
        "calendar_events_count": total,
        "pending_count": pending,
        "failed_count": failed,
        "deleted_count": deleted,
        "unlinked_calendar_event_count": unlinked,
        **sync_summary,
    }


@app.post("/calendar/retry")
@app.post("/api/calendar/retry")
def retry_calendar_sync(start: str | None = None, end: str | None = None, scopes: str = "personal,company_shared"):
    try:
        supabase.table("calendar_events").update({"sync_status": "pending"}).in_(
            "sync_status", ["failed", "pending"]
        ).execute()
    except Exception:
        pass
    return run_calendar_sync_with_lock(
        start or default_calendar_sync_start(),
        end or default_calendar_sync_end(),
        scopes,
    )


@app.get("/admin/calendar-diagnostics")
@app.get("/api/admin/calendar-diagnostics")
def get_calendar_diagnostics():
    events = table_rows(
        "calendar_events",
        "id, uid, href, calendar_scope, calendar_url, title, start_at, inspection_id, sync_status, deleted",
    )
    inspections = table_rows(
        "inspections",
        "id, date, category, calendar_event_uid, calendar_href, calendar_scope, calendar_sync_status, revision",
    )
    event_uids = {str(row.get("uid") or "") for row in events if row.get("uid")}
    event_hrefs = {str(row.get("href") or "") for row in events if row.get("href")}
    orphan_calendar_events = [
        row
        for row in events
        if not row.get("deleted") and not row.get("inspection_id")
    ][:100]
    orphan_inspections = [
        row
        for row in inspections
        if (row.get("calendar_event_uid") or row.get("calendar_href"))
        and str(row.get("calendar_event_uid") or "") not in event_uids
        and str(row.get("calendar_href") or "") not in event_hrefs
    ][:100]
    conflicts = []
    try:
        conflicts = (
            supabase.table("inspection_revision_conflicts")
            .select("*")
            .order("created_at", desc=True)
            .limit(100)
            .execute()
            .data
            or []
        )
    except Exception:
        conflicts = []
    return {
        "orphan_calendar_events": orphan_calendar_events,
        "orphan_inspections": orphan_inspections,
        "revision_conflicts": conflicts,
        "counts": {
            "orphan_calendar_events": len(orphan_calendar_events),
            "orphan_inspections": len(orphan_inspections),
            "revision_conflicts": len(conflicts),
        },
    }


@app.post("/calendar/events")
@app.post("/api/calendar/events")
def create_calendar_event(event: CalendarEventCreate):
    raise HTTPException(status_code=403, detail=CALENDAR_READ_ONLY_DETAIL)


@app.put("/calendar/events/{uid}")
@app.put("/api/calendar/events/{uid}")
def update_calendar_event(uid: str, event: CalendarEventCreate):
    raise HTTPException(status_code=403, detail=CALENDAR_READ_ONLY_DETAIL)


@app.delete("/calendar/events/{uid}")
@app.delete("/api/calendar/events/{uid}")
def delete_calendar_event(uid: str, calendar_scope: str = "company_shared"):
    raise HTTPException(status_code=403, detail=CALENDAR_READ_ONLY_DETAIL)


@app.get("/schedules")
@app.get("/api/schedules")
def get_schedules():
    try:
        schedules = (
            supabase.table("inspection_schedules")
            .select("id, company_id, date, category, time, created_at")
            .order("date", desc=True)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "inspection_schedules table is missing or unavailable. "
                "Run supabase_create_inspection_schedules.sql in Supabase. "
                f"Original error: {exc}"
            ),
        ) from exc
    rows = schedules.data or []
    company_ids = list({row.get("company_id") for row in rows if row.get("company_id")})

    companies_by_id = {}
    if company_ids:
        companies = (
            supabase.table("companies")
            .select("id, company_name")
            .in_("id", company_ids)
            .execute()
        )
        companies_by_id = {company["id"]: company for company in companies.data or []}

    for row in rows:
        company = companies_by_id.get(row.get("company_id"), {})
        row["companies"] = company
        row["company_name"] = company.get("company_name", "")

    return rows


@app.post("/schedules")
@app.post("/api/schedules")
def create_schedule(schedule: InspectionScheduleCreate):
    payload = schedule_payload_from_create(schedule)
    try:
        result = supabase.table("inspection_schedules").insert(payload).execute()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "inspection_schedules table is missing or unavailable. "
                "Run supabase_create_inspection_schedules.sql in Supabase. "
                f"Original error: {exc}"
            ),
        ) from exc
    return result.data[0]


@app.put("/schedules/{schedule_id}")
@app.put("/api/schedules/{schedule_id}")
def update_schedule(schedule_id: str, schedule: InspectionScheduleCreate):
    payload = schedule_payload_from_create(schedule)
    try:
        result = (
            supabase.table("inspection_schedules")
            .update(payload)
            .eq("id", schedule_id)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "inspection_schedules table is missing or unavailable. "
                "Run supabase_create_inspection_schedules.sql in Supabase. "
                f"Original error: {exc}"
            ),
        ) from exc
    if not result.data:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    return result.data[0]


@app.delete("/schedules/{schedule_id}")
@app.delete("/api/schedules/{schedule_id}")
def delete_schedule(schedule_id: str):
    try:
        supabase.table("inspection_schedules").delete().eq("id", schedule_id).execute()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "inspection_schedules table is missing or unavailable. "
                "Run supabase_create_inspection_schedules.sql in Supabase. "
                f"Original error: {exc}"
            ),
        ) from exc
    return {"deleted": True, "id": schedule_id}


@app.post("/inspections/sync-nas-photos")
@app.post("/api/inspections/sync-nas-photos")
def sync_nas_photos(request: NasPhotoSyncRequest):
    return sync_nas_photo_metadata(request.targets)


@app.get("/inspection-photos/{photo_id}/image")
@app.get("/api/inspection-photos/{photo_id}/image")
def get_inspection_photo_image(photo_id: str):
    photo = (
        supabase.table("inspection_photos")
        .select("id, storage_path")
        .eq("id", photo_id)
        .limit(1)
        .execute()
    )
    if not photo.data:
        raise HTTPException(status_code=404, detail="Photo not found.")

    storage_path = photo.data[0].get("storage_path", "")
    content = download_photo_from_filestation(storage_path)
    return Response(content=content, media_type="image/jpeg")
@app.post("/inspections/upload")
@app.post("/api/inspections/upload")
def upload_inspection(inspection: InspectionUpload):
    company_name = inspection.company_name.strip()
    if not company_name:
        raise HTTPException(status_code=400, detail="company_name is required.")

    if inspection.photos:
        get_filestation_config()

    company_id = ensure_company(company_name)
    inspection_id = create_inspection(company_id, inspection)

    uploaded_count = 0
    uploaded_photos = []
    for photo in inspection.photos:
        try:
            content = base64.b64decode(photo.content_base64, validate=True)
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid base64 photo data: {photo.file_name}",
            ) from exc

        nas_folder = inspection_company_folder_name(company_name)
        nas_subfolder = inspection_subfolder_name(
            date=inspection.date,
            category=inspection.category,
        )
        nas_existing_names = filestation_file_names(
            inspection_folder_path(
                company_name=company_name,
                date=inspection.date,
                category=inspection.category,
            )
        )
        nas_filename = unique_nas_filename(
            nas_folder,
            nas_subfolder,
            photo.file_name,
            nas_existing_names=nas_existing_names,
        )
        print(
            "[photo-upload] "
            f"requested_file_name={photo.file_name}, nas_filename={nas_filename}, "
            f"nas_folder={nas_folder}, nas_subfolder={nas_subfolder}, "
            f"bytes={len(content)}"
        )
        nas_path = upload_photo_to_nas(
            company_name=company_name,
            date=inspection.date,
            category=inspection.category,
            file_name=nas_filename,
            content=content,
        )

        photo_row, _, _ = upsert_inspection_photo_metadata(
            {
                "inspection_id": inspection_id,
                "facility_name": photo.facility_name,
                "photo_title": photo.photo_title,
                "file_name": nas_filename,
                "storage_path": nas_path,
                "sort_order": photo.sort_order,
                "uploaded_to_nas": True,
                "nas_folder": nas_folder,
                "nas_subfolder": nas_subfolder,
                "nas_filename": nas_filename,
                "upload_status": "uploaded",
                "upload_error": "",
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        uploaded_photos.append(
            {
                "id": str(photo_row.get("id", "")),
                "facility_name": photo.facility_name,
                "photo_title": photo.photo_title,
                "file_name": nas_filename,
                "storage_path": nas_path,
                "sort_order": photo.sort_order,
                "nas_folder": nas_folder,
                "nas_subfolder": nas_subfolder,
                "nas_filename": nas_filename,
                "upload_status": "uploaded",
            }
        )

        uploaded_count += 1

    return {
        "company_id": company_id,
        "inspection_id": inspection_id,
        "uploaded_photo_count": uploaded_count,
        "uploaded_photos": uploaded_photos,
    }


@app.post("/inspections/upload-photo")
@app.post("/api/inspections/upload-photo")
async def upload_inspection_photo(
    inspection_id: str = Form(""),
    company_name: str = Form(...),
    date: str = Form(...),
    category: str = Form(...),
    facility_name: str = Form(...),
    photo_title: str = Form(...),
    file_name: str = Form(...),
    sort_order: int = Form(0),
    local_path: str = Form(""),
    local_filename: str = Form(""),
    file: UploadFile = File(...),
):
    stage = "request_received"
    company_id = ""
    saved_inspection_id = inspection_id.strip()
    content = b""
    try:
        photo_upload_log(
            "stage=request_received "
            f"inspection_id={inspection_id}, company_name={company_name}, "
            f"file_name={file_name}, content_type={file.content_type}, "
            f"facility_name={facility_name}, photo_title={photo_title}, "
            f"sort_order={sort_order}, local_path={local_path}, "
            f"local_filename={local_filename}"
        )
        stage = "request_parse"
        company_name = company_name.strip()
        if not company_name:
            raise HTTPException(
                status_code=400,
                detail="업로드 실패(stage=request_parse): company_name is required.",
            )

        content = await file.read()
        photo_upload_log(
            "stage=parse_done "
            f"inspection_id={inspection_id}, company_name={company_name}, "
            f"file_name={file_name}, file_size={len(content)}, "
            f"content_type={file.content_type}, facility_name={facility_name}, "
            f"photo_title={photo_title}, sort_order={sort_order}"
        )

        stage = "filestation_config"
        get_filestation_config()

        stage = "ensure_company"
        company_id = ensure_company(company_name)
        photo_upload_log(
            f"stage=ensure_company done inspection_id={inspection_id}, company_id={company_id}"
        )

        upload = InspectionUpload(
            inspection_id=inspection_id,
            company_name=company_name,
            date=date,
            category=category,
            photos=[],
        )

        stage = "create_inspection"
        try:
            photo_upload_log(
                f"stage=create_inspection start inspection_id={inspection_id}, company_id={company_id}"
            )
            saved_inspection_id = create_inspection(company_id, upload)
            photo_upload_log(
                f"stage=create_inspection done inspection_id={saved_inspection_id}, company_id={company_id}"
            )
        except HTTPException:
            raise
        except Exception as exc:
            photo_upload_log(
                f"stage=create_inspection failed inspection_id={inspection_id}, company_id={company_id}, "
                f"error_type={type(exc).__name__}, error={exc}",
                error=True,
            )
            raise HTTPException(
                status_code=502,
                detail=f"점검 저장 실패(stage=create_inspection): {exc}",
            ) from exc

        nas_folder = inspection_company_folder_name(company_name)
        nas_subfolder = inspection_subfolder_name(date=date, category=category)
        stage = "nas_prepare"
        try:
            photo_upload_log(
                f"stage=nas_prepare start inspection_id={saved_inspection_id}, company_id={company_id}, "
                f"nas_folder={nas_folder}, nas_subfolder={nas_subfolder}"
            )
            nas_existing_names = filestation_file_names(
                inspection_folder_path(
                    company_name=company_name, date=date, category=category
                )
            )
            nas_filename = unique_nas_filename(
                nas_folder,
                nas_subfolder,
                file_name,
                nas_existing_names=nas_existing_names,
            )
            photo_upload_log(
                f"stage=nas_prepare done inspection_id={saved_inspection_id}, company_id={company_id}, "
                f"nas_filename={nas_filename}"
            )
        except HTTPException as exc:
            photo_upload_log(
                f"stage=nas_prepare failed inspection_id={saved_inspection_id}, company_id={company_id}, "
                f"error_type={type(exc).__name__}, error={exc.detail}",
                error=True,
            )
            raise HTTPException(
                status_code=502,
                detail=f"NAS 업로드 실패(stage=nas_prepare): {exc.detail}",
            ) from exc
        except Exception as exc:
            photo_upload_log(
                f"stage=nas_prepare failed inspection_id={saved_inspection_id}, company_id={company_id}, "
                f"error_type={type(exc).__name__}, error={exc}",
                error=True,
            )
            raise HTTPException(
                status_code=502,
                detail=f"NAS 업로드 실패(stage=nas_prepare): {exc}",
            ) from exc

        photo_upload_log(
            f"requested_file_name={file_name}, nas_filename={nas_filename}, "
            f"nas_folder={nas_folder}, nas_subfolder={nas_subfolder}, bytes={len(content)}"
        )
        base_photo_payload = {
            "inspection_id": saved_inspection_id,
            "facility_name": facility_name,
            "photo_title": photo_title,
            "file_name": nas_filename,
            "sort_order": sort_order,
            "local_path": local_path.strip(),
            "local_filename": local_filename.strip(),
            "nas_folder": nas_folder,
            "nas_subfolder": nas_subfolder,
            "nas_filename": nas_filename,
        }
        existing_uploaded_rows = []
        try:
            existing_uploaded_rows = select_inspection_photos(
                lambda columns: supabase.table("inspection_photos")
                .select(columns)
                .eq("nas_folder", nas_folder)
                .eq("nas_subfolder", nas_subfolder)
                .eq("nas_filename", nas_filename)
                .eq("upload_status", "uploaded")
                .limit(1)
            )
        except Exception as exc:
            photo_upload_log(
                f"stage=metadata_lookup skipped inspection_id={saved_inspection_id}, "
                f"company_id={company_id}, error_type={type(exc).__name__}, error={exc}",
                error=True,
            )
        if existing_uploaded_rows:
            existing_photo = existing_uploaded_rows[0]
            photo_payload = {
                **base_photo_payload,
                "storage_path": existing_photo.get("storage_path", ""),
                "uploaded_to_nas": True,
                "upload_status": "uploaded",
                "upload_error": "",
                "uploaded_at": existing_photo.get("uploaded_at"),
            }
            photo_row, metadata_saved, metadata_error = upsert_inspection_photo_metadata(
                photo_payload
            )
            return {
                "company_id": company_id,
                "inspection_id": saved_inspection_id,
                "uploaded_photo_count": 0,
                "metadata_saved": metadata_saved,
                "metadata_error": metadata_error,
                "skipped_existing": True,
                "uploaded_photos": [
                    {
                        "id": str(photo_row.get("id", existing_photo.get("id", ""))),
                        "facility_name": facility_name,
                        "photo_title": photo_title,
                        "file_name": nas_filename,
                        "storage_path": existing_photo.get("storage_path", ""),
                        "sort_order": sort_order,
                        "local_path": local_path.strip(),
                        "local_filename": local_filename.strip(),
                        "nas_folder": nas_folder,
                        "nas_subfolder": nas_subfolder,
                        "nas_filename": nas_filename,
                        "upload_status": "uploaded",
                        "upload_error": "",
                        "uploaded_at": existing_photo.get("uploaded_at"),
                    }
                ],
            }

        stage = "metadata_uploading"
        upsert_inspection_photo_metadata(
            {
                **base_photo_payload,
                "storage_path": "",
                "uploaded_to_nas": False,
                "upload_status": "uploading",
                "upload_error": "",
            }
        )

        stage = "nas_upload"
        try:
            photo_upload_log(
                f"stage=nas_upload start inspection_id={saved_inspection_id}, company_id={company_id}, "
                f"nas_filename={nas_filename}, bytes={len(content)}"
            )
            nas_path = upload_photo_to_nas(
                company_name=company_name,
                date=date,
                category=category,
                file_name=nas_filename,
                content=content,
            )
            photo_upload_log(
                f"stage=nas_upload done inspection_id={saved_inspection_id}, company_id={company_id}, nas_path={nas_path}"
            )
        except HTTPException as exc:
            upload_error = f"NAS 업로드 실패(stage=nas_upload): {exc.detail}"
            photo_upload_log(
                f"stage=nas_upload failed inspection_id={saved_inspection_id}, company_id={company_id}, "
                f"error_type={type(exc).__name__}, error={exc.detail}",
                error=True,
            )
            failed_payload = {
                **base_photo_payload,
                "storage_path": "",
                "uploaded_to_nas": False,
                "upload_status": "failed",
                "upload_error": upload_error,
            }
            upsert_inspection_photo_metadata(failed_payload)
            raise HTTPException(status_code=502, detail=upload_error) from exc
        except Exception as exc:
            upload_error = f"NAS 업로드 실패(stage=nas_upload): {exc}"
            photo_upload_log(
                f"stage=nas_upload failed inspection_id={saved_inspection_id}, company_id={company_id}, "
                f"error_type={type(exc).__name__}, error={exc}",
                error=True,
            )
            failed_payload = {
                **base_photo_payload,
                "storage_path": "",
                "uploaded_to_nas": False,
                "upload_status": "failed",
                "upload_error": upload_error,
            }
            upsert_inspection_photo_metadata(failed_payload)
            raise HTTPException(status_code=502, detail=upload_error) from exc

        stage = "metadata_uploaded"
        photo_payload = {
            **base_photo_payload,
            "storage_path": nas_path,
            "uploaded_to_nas": True,
            "upload_status": "uploaded",
            "upload_error": "",
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        }
        photo_row, metadata_saved, metadata_error = upsert_inspection_photo_metadata(
            photo_payload
        )

        return {
            "company_id": company_id,
            "inspection_id": saved_inspection_id,
            "uploaded_photo_count": 1,
            "metadata_saved": metadata_saved,
            "metadata_error": metadata_error,
            "uploaded_photos": [
                {
                    "id": str(photo_row.get("id", "")),
                    "facility_name": facility_name,
                    "photo_title": photo_title,
                    "file_name": nas_filename,
                    "storage_path": nas_path,
                    "sort_order": sort_order,
                    "local_path": local_path.strip(),
                    "local_filename": local_filename.strip(),
                    "nas_folder": nas_folder,
                    "nas_subfolder": nas_subfolder,
                    "nas_filename": nas_filename,
                    "upload_status": "uploaded",
                    "upload_error": "",
                }
            ],
        }
    except HTTPException as exc:
        photo_upload_log(
            f"stage={stage} http_failed inspection_id={saved_inspection_id}, company_id={company_id}, "
            f"error_type={type(exc).__name__}, error={exc.detail}",
            error=True,
        )
        raise
    except Exception as exc:
        tb = traceback.format_exc()
        photo_upload_log(
            f"stage=unexpected_failed last_stage={stage}, inspection_id={saved_inspection_id}, "
            f"company_id={company_id}, error_type={type(exc).__name__}, error={exc}, traceback={tb}",
            error=True,
        )
        raise HTTPException(
            status_code=502,
            detail=f"업로드 실패(stage=unexpected_failed,last_stage={stage}): {exc}",
        ) from exc



