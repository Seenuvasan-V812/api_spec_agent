from fastapi import APIRouter, Depends, Header

from store.models import Order, OrderItem
from store.security import get_current_user
from store.services import place_order_for

router = APIRouter(prefix="/orders", tags=["orders"], dependencies=[Depends(get_current_user)])


@router.post("", response_model=Order, status_code=201)
def place_order(item: OrderItem, idempotency_key: str = Header(..., alias="Idempotency-Key")):
    """Place an order for a pet."""
    order_id = place_order_for(item.pet_id, item.quantity)
    return Order(id=order_id, items=[item], total_cents=0, placed_at=None)


@router.get("/{order_id}", response_model=Order)
def get_order(order_id: int):
    """Fetch an order."""
    return None
