from pydantic import BaseModel, Field
from fastapi import FastAPI

app = FastAPI(title="Checkout Service")


class CartItem(BaseModel):
    sku: str
    quantity: int = Field(default=1, ge=1)


class CheckoutRequest(BaseModel):
    items: list[CartItem]
    coupon: str | None = None


class CheckoutResult(BaseModel):
    order_id: str
    total_cents: int


@app.post("/checkout", response_model=CheckoutResult, status_code=201)
def checkout(payload: CheckoutRequest):
    """Start a checkout for the given cart."""
    return CheckoutResult(order_id="o-1", total_cents=0)
