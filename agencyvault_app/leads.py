from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from agencyvault_app.database import engine

router = APIRouter()

class LeadCreate(BaseModel):
    first_name: str
    last_name: str
    phone: str

@router.post("/leads")
async def create_lead(lead: LeadCreate):
    try:
        async with engine.begin() as conn:
            result = await conn.execute(
                text("""
                    INSERT INTO leads (first_name, last_name, phone, status)
                    VALUES (:first_name, :last_name, :phone, 'new')
                    RETURNING id
                """),
                {
                    "first_name": lead.first_name,
                    "last_name": lead.last_name,
                    "phone": lead.phone,
                }
            )
            lead_id = result.scalar()

        return {"id": str(lead_id), "status": "saved"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
