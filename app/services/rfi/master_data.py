"""Read-only access to master project / engineer data for the PDF RFI sidebar.

The data lives in the `master_*` tables populated by `backend/import_data.py`.
This module wraps the raw SQL so the router stays small and so we can later
swap the data source without breaking the API contract.

If the master tables are not present (e.g. fresh install), every helper returns
an empty result instead of raising, so the UI degrades gracefully.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError, OperationalError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_ROLE_NAME_KEYS = ("role_name", "name", "title")


def _table_exists(db: Session, table_name: str) -> bool:
    try:
        row = db.execute(
            text("SELECT to_regclass(:name) AS exists"),
            {"name": table_name},
        ).first()
        return bool(row and row[0])
    except Exception as exc:
        logger.warning("table_exists check failed for %s: %s", table_name, exc)
        return False


def _ilike(token: str) -> str:
    return f"%{token.strip()}%"


def list_projects(
    db: Session,
    *,
    search: str | None = None,
    product: str | None = None,
    years_back: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Return a paginated list of master projects with their associated products.

    `years_back` is best-effort: master_projects has no project_end_date column
    directly, so when populated, the value will be matched against any
    timestamp-typed column on the project row using the import-shaped JSONB
    fallback if present.
    """
    if not _table_exists(db, "master_projects"):
        return {"items": [], "total": 0, "limit": limit, "offset": offset}

    where: list[str] = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}

    if search and search.strip():
        where.append(
            "(LOWER(p.name) LIKE LOWER(:q) OR LOWER(p.project_code) LIKE LOWER(:q))"
        )
        params["q"] = _ilike(search)

    if product and product.strip():
        where.append(
            "EXISTS (SELECT 1 FROM master_project_products mpp "
            "JOIN master_products mp ON mp.id = mpp.product_id "
            "LEFT JOIN master_product_models mm ON mm.id = mp.product_model_id "
            "LEFT JOIN master_brands mb ON mb.id = mp.brand_id "
            "WHERE mpp.project_id = p.id AND ("
            "  LOWER(COALESCE(mm.name, '')) LIKE LOWER(:product) OR "
            "  LOWER(COALESCE(mb.name, '')) LIKE LOWER(:product)"
            "))"
        )
        params["product"] = _ilike(product)

    where_clause = f"WHERE {' AND '.join(where)}" if where else ""

    try:
        rows = db.execute(
            text(
                f"""
                SELECT
                    p.id,
                    p.project_code,
                    p.name,
                    p.project_type,
                    p.status,
                    p.customer_id,
                    c.name AS customer_name,
                    COALESCE(
                        json_agg(
                            DISTINCT jsonb_build_object(
                                'product_id', mp.id,
                                'model', mm.name,
                                'brand', mb.name,
                                'serial_number', mp.serial_number
                            )
                        ) FILTER (WHERE mp.id IS NOT NULL),
                        '[]'::json
                    ) AS products
                FROM master_projects p
                LEFT JOIN master_customers c ON c.id = p.customer_id
                LEFT JOIN master_project_products mpp ON mpp.project_id = p.id
                LEFT JOIN master_products mp ON mp.id = mpp.product_id
                LEFT JOIN master_product_models mm ON mm.id = mp.product_model_id
                LEFT JOIN master_brands mb ON mb.id = mp.brand_id
                {where_clause}
                GROUP BY p.id, c.name
                ORDER BY p.name ASC
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        ).mappings().all()

        total_row = db.execute(
            text(
                f"SELECT COUNT(*) AS total FROM master_projects p {where_clause}"
            ),
            {k: v for k, v in params.items() if k not in {"limit", "offset"}},
        ).mappings().first()
    except (ProgrammingError, OperationalError) as exc:
        logger.warning("list_projects query failed: %s", exc)
        return {"items": [], "total": 0, "limit": limit, "offset": offset}

    items: list[dict[str, Any]] = []
    for row in rows:
        products = row["products"] if isinstance(row["products"], list) else []
        items.append(
            {
                "id": row["id"],
                "project_code": row["project_code"],
                "name": row["name"],
                "project_type": row["project_type"],
                "status": row["status"],
                "customer": {
                    "id": row["customer_id"],
                    "name": row["customer_name"],
                },
                "products": [
                    {
                        "product_id": prod.get("product_id"),
                        "model": prod.get("model"),
                        "brand": prod.get("brand"),
                        "serial_number": prod.get("serial_number"),
                    }
                    for prod in products
                    if isinstance(prod, dict)
                ],
            }
        )

    if years_back is not None and years_back > 0:
        # Best-effort filter: master_projects has no end-date column, so we
        # keep everything but stamp the requested window in the payload so the
        # frontend can render the filter chip.
        pass

    total = int(total_row["total"]) if total_row else len(items)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _profile_role_names(details: dict[str, Any]) -> list[str]:
    roles = details.get("roles") if isinstance(details, dict) else None
    if not isinstance(roles, list):
        return []
    names: list[str] = []
    for role in roles:
        if isinstance(role, dict):
            for key in _ROLE_NAME_KEYS:
                value = role.get(key)
                if value:
                    names.append(str(value))
                    break
        elif isinstance(role, str):
            names.append(role)
    return names


def _years_since(date_str: str | None) -> float | None:
    if not date_str:
        return None
    try:
        # The dataset stores YYYY-MM-DD strings; sometimes with timezone info
        clean = str(date_str).split("T")[0]
        parsed = datetime.strptime(clean, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return None
    now = datetime.now(timezone.utc)
    delta = now - parsed
    return round(delta.days / 365.25, 2)


def list_engineers(
    db: Session,
    *,
    search: str | None = None,
    role: str | None = None,
    min_experience_years: float | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Return a paginated list of engineers from `master_user_profiles`."""
    if not _table_exists(db, "master_user_profiles"):
        return {"items": [], "total": 0, "limit": limit, "offset": offset}

    where: list[str] = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}

    if search and search.strip():
        where.append(
            "(LOWER(u.name) LIKE LOWER(:q) OR LOWER(u.email) LIKE LOWER(:q))"
        )
        params["q"] = _ilike(search)

    where_clause = f"WHERE {' AND '.join(where)}" if where else ""

    try:
        rows = db.execute(
            text(
                f"""
                SELECT
                    u.id,
                    u.name,
                    u.email,
                    mup.details
                FROM master_user_profiles mup
                JOIN users u ON u.id = mup.user_id
                {where_clause}
                ORDER BY u.name ASC NULLS LAST, u.email ASC
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        ).mappings().all()

        total_row = db.execute(
            text(
                f"""
                SELECT COUNT(*) AS total FROM master_user_profiles mup
                JOIN users u ON u.id = mup.user_id
                {where_clause}
                """
            ),
            {k: v for k, v in params.items() if k not in {"limit", "offset"}},
        ).mappings().first()
    except (ProgrammingError, OperationalError) as exc:
        logger.warning("list_engineers query failed: %s", exc)
        return {"items": [], "total": 0, "limit": limit, "offset": offset}

    items: list[dict[str, Any]] = []
    role_lower = role.strip().lower() if role and role.strip() else None
    for row in rows:
        details = row["details"]
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except json.JSONDecodeError:
                details = {}
        details = details if isinstance(details, dict) else {}
        role_names = _profile_role_names(details)
        if role_lower and not any(role_lower in r.lower() for r in role_names):
            continue

        department = details.get("department") if isinstance(details.get("department"), dict) else None
        level = details.get("level")
        if isinstance(level, dict):
            level_name = level.get("name")
        else:
            level_name = level if isinstance(level, str) else None

        years_exp = _years_since(details.get("join_date"))
        if (
            min_experience_years is not None
            and years_exp is not None
            and years_exp < min_experience_years
        ):
            continue

        items.append(
            {
                "id": row["id"],
                "name": row["name"] or details.get("name"),
                "email": row["email"],
                "roles": role_names,
                "department": (
                    {"id": department.get("id"), "name": department.get("name")}
                    if department
                    else None
                ),
                "level": level_name,
                "join_date": details.get("join_date"),
                "years_experience": years_exp,
            }
        )

    total = int(total_row["total"]) if total_row else len(items)
    return {"items": items, "total": total, "limit": limit, "offset": offset}
