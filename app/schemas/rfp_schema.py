from typing import Optional
from pydantic import BaseModel, Field


class GenerateTechnicalContentRequest(BaseModel):
    product: str = Field(..., description="The product/technology being proposed")
    rfp: bool = Field(True, description="Must be true for RFP generation")
    adjust: Optional[bool] = Field(None, description="Set true to adjust/refine existing content")
    content: Optional[str] = Field(None, description="Current content to adjust (required when adjust=true)")
    projectName: Optional[str] = Field(None, description="Name of the project / proposal")
    projectDescription: Optional[str] = Field(None, description="Brief project description")
    additionalContext: Optional[str] = Field(None, description="Extra context or constraints")
