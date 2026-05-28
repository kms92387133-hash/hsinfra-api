import base64
import uuid
import io
import os
import re
from datetime import datetime, timedelta, timezone
from typing import List
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, build_opener, HTTPBasicAuthHandler, HTTPPasswordMgrWithDefaultRealm
import xml.etree.ElementTree as ET
import zipfile

import caldav
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from supabase import create_client

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)

app = FastAPI()

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


class CompanyCreate(BaseModel):
    company_name: str
    address: str = ""
    address_group: str = ""
    building_type: str = ""
    manager: str = ""
    phone: str = ""
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
    date: str
    category: str


class InspectionScheduleCreate(BaseModel):
    company_name: str
    date: str
    category: str
    time: str = ""


class CalendarEventCreate(BaseModel):
    company_name: str
    title: str
    start_at: str
    end_at: str
    memo: str = ""
    location: str = ""


def clean_path_segment(value: str) -> str:
    cleaned = "".join("_" if ch in '\\/:*?"<>|' else ch for ch in value).strip()
    return cleaned or "_"


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
    link_id = os.getenv("SPREADSHEET_LINK_ID", "18G9kPV3pD1fRRwbtEHtvXlbDjxEkF3r")
    sheet_id = int(os.getenv("SPREADSHEET_SHEET_ID", "3"))
    return base_url, link_id, sheet_id


def spreadsheet_sheet_name() -> str:
    return os.getenv("SPREADSHEET_SHEET_NAME", "app").strip() or "app"


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
    contract_manager_index = find_header_index(headers, ["계약담당자"])
    note_index = find_header_index(headers, ["특이사항/ 3일전협의", "특이사항", "메모"])

    if company_index is None:
        return lookup

    for row in rows[1:]:
        company_name = value_at(row, company_index)
        if not company_name:
            continue

        contacts = []
        seen = set()
        append_contact(
            contacts,
            seen,
            label="담당자",
            name=value_at(row, contract_manager_index),
        )

        note = value_at(row, note_index)
        if note:
            contacts.append(f"메모: {note}")

        if contacts:
            lookup[company_name] = "\n".join(contacts)

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
        "building_type": ["건물구분", "건물유형", "구분"],
        "manager": ["담당자", "담당자1", "관리자"],
        "phone": ["연락처", "연락처1", "전화번호"],
    }
    fallback_columns = {
        "company_name": 2,
        "address": 3,
        "address_group": 4,
        "building_type": None,
        "manager": 8,
        "phone": 9,
    }

    column_map = {}
    for field, names in aliases.items():
        found = None
        for name in names:
            normalized = normalize_header(name)
            if normalized in headers:
                found = headers[normalized]
                break
        column_map[field] = found if found is not None else fallback_columns[field]

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
        companies.append(
            {
                "company_name": company_name,
                "address": value_at(row, column_map["address"]),
                "address_group": value_at(row, column_map["address_group"]),
                "building_type": building_type,
                "manager": value_at(row, column_map["manager"]),
                "phone": normalize_phone(value_at(row, column_map["phone"])),
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
        }
    return set(result.data[0].keys())


def upsert_companies(companies: list[dict]) -> dict:
    inserted = 0
    updated = 0
    allowed_columns = company_column_names()
    for company in companies:
        company_payload = {
            key: value for key, value in company.items() if key in allowed_columns
        }
        existing = (
            supabase.table("companies")
            .select("id")
            .eq("company_name", company["company_name"])
            .limit(1)
            .execute()
        )
        if existing.data:
            update_payload = {
                key: value
                for key, value in company_payload.items()
                if key == "company_name" or str(value).strip()
            }
            supabase.table("companies").update(update_payload).eq(
                "id", existing.data[0]["id"]
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


def filestation_login() -> tuple[str, str]:
    base_url, username, password, _ = get_filestation_config()
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
        raise HTTPException(
            status_code=502,
            detail=f"NAS File Station login failed: {payload}",
        )

    return base_url, payload["data"]["sid"]


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
        response = requests.post(
            f"{base_url}/webapi/entry.cgi",
            params={
                "api": "SYNO.FileStation.Upload",
                "version": "2",
                "method": "upload",
                "_sid": sid,
            },
            data={
                "api": "SYNO.FileStation.Upload",
                "version": "2",
                "method": "upload",
                "path": upload_dir,
                "create_parents": "true",
                "overwrite": "true",
                "_sid": sid,
            },
            files={
                "file": (safe_file_name, content, "image/jpeg"),
            },
            timeout=120,
        )
        response.raise_for_status()
        payload = response.json()

        if not payload.get("success"):
            raise HTTPException(
                status_code=502,
                detail=f"NAS File Station upload failed: {payload}",
            )

        return nas_path
    finally:
        filestation_logout(base_url, sid)


def inspection_folder_name(*, company_name: str, date: str, category: str) -> str:
    return clean_path_segment(f"{compact_date(date)} ({category}) {company_name}")


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
) -> None:
    base_url, username, password = get_nas_config()
    opener = webdav_opener(base_url, username, password)
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type

    request = Request(
        webdav_url(base_url, relative_path),
        data=data,
        headers=headers,
        method=method,
    )

    try:
        with opener.open(request, timeout=30) as response:
            if response.status >= 400:
                raise HTTPException(
                    status_code=502,
                    detail=f"NAS WebDAV {method} failed: HTTP {response.status}",
                )
    except HTTPError as exc:
        if method == "MKCOL" and exc.code in (301, 405):
            return
        raise HTTPException(
            status_code=502,
            detail=f"NAS WebDAV {method} failed: HTTP {exc.code}",
        ) from exc


def ensure_nas_dirs(relative_dir: str) -> None:
    current = ""
    for part in [item for item in relative_dir.split("/") if item]:
        current = f"{current}/{part}" if current else part
        webdav_request("MKCOL", current)


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
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"NAS File Station upload failed: {exc}",
        ) from exc


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
    if inspection.inspection_id.strip():
        supabase.table("inspections").update(
            {
                "company_id": company_id,
                "date": inspection.date,
                "category": inspection.category,
            }
        ).eq("id", inspection.inspection_id.strip()).execute()
        return inspection.inspection_id.strip()

    inserted = (
        supabase.table("inspections")
        .insert(
            {
                "company_id": company_id,
                "date": inspection.date,
                "category": inspection.category,
            }
        )
        .execute()
    )

    return str(inserted.data[0]["id"])


def inspection_payload_from_create(inspection: InspectionCreate) -> dict:
    company_name = inspection.company_name.strip()
    if not company_name:
        raise HTTPException(status_code=400, detail="company_name is required.")
    company_id = ensure_company(company_name)
    return {
        "company_id": company_id,
        "date": inspection.date,
        "category": inspection.category,
    }



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


def synology_calendar():
    url, username, password = caldav_config()
    base_url = url.rstrip("/")
    candidate_urls = []

    def add_candidate(candidate: str):
        normalized = candidate.rstrip("/") + "/"
        if normalized not in candidate_urls:
            candidate_urls.append(normalized)

    add_candidate(url)
    add_candidate(f"{base_url}/{username}/home/")
    add_candidate(f"{base_url}/{username}/")

    try:
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
                for cal in calendars:
                    if str(cal.url).rstrip("/").endswith("/home"):
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


def escape_ics_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def unfold_ics(data: str) -> str:
    return re.sub(r"\r?\n[ \t]", "", data or "")


def ics_field(data: str, name: str) -> str:
    match = re.search(rf"^{name}(?:;[^:]*)?:(.*)$", data, re.MULTILINE)
    return match.group(1).strip() if match else ""


def parse_ics_datetime(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    try:
        if raw.endswith("Z"):
            parsed = datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return parsed.isoformat()
        if "T" in raw:
            return datetime.strptime(raw[:15], "%Y%m%dT%H%M%S").isoformat()
        return datetime.strptime(raw[:8], "%Y%m%d").date().isoformat()
    except Exception:
        return raw


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


def calendar_event_object(uid: str):
    calendar = synology_calendar()
    try:
        return calendar.event_by_uid(uid)
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Synology CalDAV event not found: {exc}",
        ) from exc


def calendar_event_to_json(event) -> dict:
    data = unfold_ics(getattr(event, "data", "") or "")
    memo = ics_field(data, "DESCRIPTION").replace("\\n", "\n")
    company_name = ""
    for line in memo.splitlines():
        if line.startswith("업체명:"):
            company_name = line.split(":", 1)[1].strip()
            break
    return {
        "uid": ics_field(data, "UID"),
        "company_name": company_name,
        "title": ics_field(data, "SUMMARY"),
        "memo": memo,
        "location": ics_field(data, "LOCATION"),
        "start_at": parse_ics_datetime(ics_field(data, "DTSTART")),
        "end_at": parse_ics_datetime(ics_field(data, "DTEND")),
    }


def create_synology_calendar_event(
    event: CalendarEventCreate,
    uid_override: str | None = None,
) -> dict:
    calendar = synology_calendar()
    start_at = parse_event_datetime(event.start_at)
    end_at = parse_event_datetime(event.end_at)
    if end_at <= start_at:
        raise HTTPException(status_code=400, detail="end_at must be after start_at.")

    uid = uid_override or f"{uuid.uuid4()}@hsinfra"
    title = event.title.strip() or event.company_name.strip() or "일정"
    description = event.memo.strip()
    company_name = event.company_name.strip()
    if company_name:
        description = f"업체명: {company_name}\n{description}".strip()
    location = event.location.strip() or company_address_for_calendar(company_name)

    ics = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//HS Infra Inspection App//Synology Calendar//KO",
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
            format_ics_datetime_line("DTSTART", start_at),
            format_ics_datetime_line("DTEND", end_at),
            f"SUMMARY:{escape_ics_text(title)}",
            f"DESCRIPTION:{escape_ics_text(description)}",
            *([f"LOCATION:{escape_ics_text(location)}"] if location else []),
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
        "url": str(getattr(saved, "url", "")),
    }


def update_synology_calendar_event(uid: str, event: CalendarEventCreate) -> dict:
    existing = calendar_event_object(uid)
    try:
        existing.delete()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Synology CalDAV event delete before update failed: {exc}",
        ) from exc
    return create_synology_calendar_event(event, uid_override=uid)


def delete_synology_calendar_event(uid: str) -> dict:
    existing = calendar_event_object(uid)
    try:
        existing.delete()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Synology CalDAV event delete failed: {exc}",
        ) from exc
    return {"deleted": True, "uid": uid}


def list_synology_calendar_events(start: str, end: str) -> list[dict]:
    calendar = synology_calendar()
    start_at = parse_event_datetime(start)
    end_at = parse_event_datetime(end)
    if end_at <= start_at:
        raise HTTPException(status_code=400, detail="end must be after start.")
    try:
        events = calendar.date_search(start=start_at, end=end_at)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Synology CalDAV event query failed: {exc}",
        ) from exc
    return [calendar_event_to_json(event) for event in events]


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
        .order("created_at", desc=True)
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
        .select("id, company_id, date, category, created_at")
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
        photos = (
            supabase.table("inspection_photos")
            .select(
                "id, inspection_id, facility_name, photo_title, file_name, storage_path, sort_order, uploaded_to_nas"
            )
            .in_("inspection_id", inspection_ids)
            .execute()
        )
        for photo in photos.data or []:
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
    result = supabase.table("inspections").insert(payload).execute()
    return result.data[0]


@app.put("/inspections/{inspection_id}")
@app.put("/api/inspections/{inspection_id}")
def update_inspection_record(inspection_id: str, inspection: InspectionCreate):
    existing = (
        supabase.table("inspections")
        .select("id, company_id, date, category")
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

    payload = inspection_payload_from_create(inspection)
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
    supabase.table("inspection_photos").delete().eq(
        "inspection_id", inspection_id
    ).execute()
    supabase.table("inspections").delete().eq("id", inspection_id).execute()
    return {"deleted": True, "id": inspection_id}


@app.get("/calendar/check")
@app.get("/api/calendar/check")
def check_calendar_connection():
    cal = synology_calendar()
    return {
        "success": True,
        "message": "Synology Calendar connection successful.",
        "calendar_name": getattr(cal, "name", "Unknown"),
        "calendar_url": str(getattr(cal, "url", ""))
    }


@app.get("/calendar/events")
@app.get("/api/calendar/events")
def get_calendar_events(start: str, end: str):
    return list_synology_calendar_events(start, end)


@app.post("/calendar/events")
@app.post("/api/calendar/events")
def create_calendar_event(event: CalendarEventCreate):
    return create_synology_calendar_event(event)


@app.put("/calendar/events/{uid}")
@app.put("/api/calendar/events/{uid}")
def update_calendar_event(uid: str, event: CalendarEventCreate):
    return update_synology_calendar_event(uid, event)


@app.delete("/calendar/events/{uid}")
@app.delete("/api/calendar/events/{uid}")
def delete_calendar_event(uid: str):
    return delete_synology_calendar_event(uid)


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

        nas_path = upload_photo_to_nas(
            company_name=company_name,
            date=inspection.date,
            category=inspection.category,
            file_name=photo.file_name,
            content=content,
        )

        inserted_photo = (
            supabase.table("inspection_photos")
            .upsert(
                {
                    "inspection_id": inspection_id,
                    "facility_name": photo.facility_name,
                    "photo_title": photo.photo_title,
                    "file_name": photo.file_name,
                    "storage_path": nas_path,
                    "sort_order": photo.sort_order,
                    "uploaded_to_nas": True,
                },
                on_conflict="inspection_id,facility_name,sort_order",
            )
            .execute()
        )
        photo_row = inserted_photo.data[0] if inserted_photo.data else {}
        uploaded_photos.append(
            {
                "id": str(photo_row.get("id", "")),
                "facility_name": photo.facility_name,
                "photo_title": photo.photo_title,
                "file_name": photo.file_name,
                "storage_path": nas_path,
                "sort_order": photo.sort_order,
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
    file: UploadFile = File(...),
):
    company_name = company_name.strip()
    if not company_name:
        raise HTTPException(status_code=400, detail="company_name is required.")

    get_filestation_config()
    company_id = ensure_company(company_name)
    upload = InspectionUpload(
        inspection_id=inspection_id,
        company_name=company_name,
        date=date,
        category=category,
        photos=[],
    )
    saved_inspection_id = create_inspection(company_id, upload)
    content = await file.read()

    nas_path = upload_photo_to_nas(
        company_name=company_name,
        date=date,
        category=category,
        file_name=file_name,
        content=content,
    )

    photo_payload = {
        "inspection_id": saved_inspection_id,
        "facility_name": facility_name,
        "photo_title": photo_title,
        "file_name": file_name,
        "storage_path": nas_path,
        "sort_order": sort_order,
        "uploaded_to_nas": True,
    }
    photo_row = {}
    metadata_saved = False
    metadata_error = ""

    try:
        inserted_photo = (
            supabase.table("inspection_photos")
            .upsert(
                photo_payload,
                on_conflict="inspection_id,facility_name,sort_order",
            )
            .execute()
        )
        photo_row = inserted_photo.data[0] if inserted_photo.data else {}
        metadata_saved = True
    except Exception as exc:
        metadata_error = str(exc)
        try:
            existing_photo = (
                supabase.table("inspection_photos")
                .select("id")
                .eq("inspection_id", saved_inspection_id)
                .eq("facility_name", facility_name)
                .eq("sort_order", sort_order)
                .limit(1)
                .execute()
            )
            if existing_photo.data:
                photo_id = existing_photo.data[0]["id"]
                updated_photo = (
                    supabase.table("inspection_photos")
                    .update(photo_payload)
                    .eq("id", photo_id)
                    .execute()
                )
                photo_row = updated_photo.data[0] if updated_photo.data else {"id": photo_id}
            else:
                inserted_photo = (
                    supabase.table("inspection_photos")
                    .insert(photo_payload)
                    .execute()
                )
                photo_row = inserted_photo.data[0] if inserted_photo.data else {}
            metadata_saved = True
            metadata_error = ""
        except Exception as fallback_exc:
            metadata_error = f"{metadata_error}; fallback failed: {fallback_exc}"

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
                "file_name": file_name,
                "storage_path": nas_path,
                "sort_order": sort_order,
            }
        ],
    }
