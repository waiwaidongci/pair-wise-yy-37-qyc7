from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
class ErrorKind:
    VALIDATION="validation"; NOT_FOUND="not_found"; FORBIDDEN="forbidden"; CONFLICT="conflict"
class DomainError(Exception):
    kind=ErrorKind.VALIDATION
    def __init__(self,message): super().__init__(message); self.message=message
class ValidationError(DomainError): kind=ErrorKind.VALIDATION
class NotFoundError(DomainError): kind=ErrorKind.NOT_FOUND
class PermissionDenied(DomainError): kind=ErrorKind.FORBIDDEN
class ConflictError(DomainError): kind=ErrorKind.CONFLICT
SEVERITIES=['low', 'medium', 'high', 'critical']; STATES=['draft', 'submitted', 'inspection', 'correction', 'approved']; ROLES=['applicant', 'inspector', 'compliance_manager', 'viewer']
RENEWAL_STATUSES=['submitted', 'returned', 'approved']
@dataclass(frozen=True)
class Item:
    id:int; title:str; description:str; severity:str; quantity:float; threshold:float; status:str; version:int; external_ref:Optional[str]; valid_from:Optional[str]; valid_until:Optional[str]; created_by:str; created_at:str; updated_at:str
@dataclass(frozen=True)
class Record:
    id:int; item_id:int; kind:str; detail:str; status:str; external_ref:Optional[str]; created_by:str; created_at:str
@dataclass(frozen=True)
class AuditEntry:
    id:int; action:str; entity_type:str; entity_id:int; actor:str; detail:Dict[str,Any]; previous_hash:str; entry_hash:str; created_at:str
def require_text(value,field,max_length=2000):
    if not isinstance(value,str) or not value.strip(): raise ValidationError(f"{field}不能为空")
    value=value.strip()
    if len(value)>max_length: raise ValidationError(f"{field}不能超过{max_length}个字符")
    return value
def normalize_severity(value):
    if value not in SEVERITIES: raise ValidationError("severity不在允许范围内")
    return value
def require_number(value,field,minimum=0.0):
    if isinstance(value,bool): raise ValidationError(f"{field}必须是数字")
    try: number=float(value)
    except (TypeError,ValueError): raise ValidationError(f"{field}必须是数字")
    if number<minimum: raise ValidationError(f"{field}不能小于{minimum}")
    return number
def ensure_role(role,allowed):
    if role not in allowed: raise PermissionDenied("当前角色无权执行该操作")
def parse_timepoint(value, field, end_of_day=False):
    """解析ISO日期或日期时间，归一化为带UTC时区的datetime；纯日期按当天起止处理。"""
    value=require_text(value, field, 40)
    text=value.strip()
    try:
        if len(text)==10:
            day=datetime.strptime(text, "%Y-%m-%d")
            if end_of_day: day=day.replace(hour=23, minute=59, second=59)
            return day.replace(tzinfo=timezone.utc)
        normalized=text.replace("Z", "+00:00")
        parsed=datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValidationError(f"{field}必须是YYYY-MM-DD或ISO日期时间") from exc
    if parsed.tzinfo is None: parsed=parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
def require_period(valid_from, valid_until, require_future_end=False):
    """校验并返回(起始, 截止)文本；纯日期保留原始文本以便展示。"""
    if valid_from is None or valid_until is None:
        raise ValidationError("valid_from和valid_until必须同时提供")
    start_text=valid_from.strip(); end_text=valid_until.strip()
    start=parse_timepoint(valid_from, "valid_from")
    end=parse_timepoint(valid_until, "valid_until", end_of_day=True)
    if end<=start: raise ValidationError("valid_until必须晚于valid_from")
    if require_future_end and end<=datetime.now(timezone.utc):
        raise ValidationError("新期限截止时间必须晚于当前时间")
    return start_text, end_text
def require_materials(value):
    """续期材料：非空数组，每项含name，detail可缺省（沿用name）。"""
    if not isinstance(value, list) or not value:
        raise ValidationError("续期材料不能为空")
    materials:List[Dict[str,str]]=[]
    for entry in value:
        if not isinstance(entry, dict): raise ValidationError("材料格式不正确")
        name=require_text(entry.get("name"), "材料名称", 200)
        detail=entry.get("detail")
        detail=require_text(detail, "材料说明") if detail is not None else name
        materials.append({"name": name, "detail": detail})
    if not materials: raise ValidationError("续期材料不能为空")
    return materials
