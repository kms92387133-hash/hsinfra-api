import base64
import io
import os
import re
from typing import List
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, build_opener, HTTPBasicAuthHandler, HTTPPasswordMgrWithDefaultRealm
import xml.etree.ElementTree as ET
import zipfile

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
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
    company_name: str
    date: str
    category: str
    photos: List[InspectionPhotoUpload] = Field(default_factory=list)


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


def read_xlsx_sheet_rows(xlsx_bytes: bytes, sheet_id: int) -> list[list[str]]:
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(xlsx_bytes)) as zip_file:
        shared_strings = xlsx_shared_strings(zip_file)
        root = ET.fromstring(zip_file.read(f"xl/worksheets/sheet{sheet_id}.xml"))
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
    for sheet_id in (1, 2):
        rows = read_xlsx_sheet_rows(xlsx_bytes, sheet_id)
        if not rows:
            continue

        company_index = find_header_index(rows[0], ["업체명", "회사명", "업체", "회사"])
        building_type_index = find_header_index(rows[0], ["건물유형", "건물구분"])

        if company_index is None or building_type_index is None:
            continue

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
    for sheet_id in (1, 2):
        rows = read_xlsx_sheet_rows(xlsx_bytes, sheet_id)
        if not rows:
            continue

        headers = rows[0]
        company_index = find_header_index(headers, ["업체명", "회사명", "업체", "회사"])
        contract_manager_index = find_header_index(headers, ["계약담당자"])
        note_index = find_header_index(headers, ["특이사항/ 3일전협의", "특이사항", "메모"])

        if company_index is None:
            continue

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
    _, _, sheet_id = spreadsheet_config()
    rows = read_xlsx_sheet_rows(xlsx_bytes, sheet_id)
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
    inspection_dir = clean_path_segment(f"{compact_date(date)} ({category}) {company_name}")
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
                "path": upload_dir,
                "create_parents": "true",
                "overwrite": "true",
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

        supabase.table("inspection_photos").insert(
            {
                "inspection_id": inspection_id,
                "facility_name": photo.facility_name,
                "photo_title": photo.photo_title,
                "storage_path": nas_path,
                "sort_order": photo.sort_order,
            }
        ).execute()

        uploaded_count += 1

    return {
        "company_id": company_id,
        "inspection_id": inspection_id,
        "uploaded_photo_count": uploaded_count,
    }
