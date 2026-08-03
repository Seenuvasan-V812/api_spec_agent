from pydantic import BaseModel
from fastapi import FastAPI, HTTPException

app = FastAPI(title="Catalog Service")


class Product(BaseModel):
    sku: str
    name: str
    price_cents: int


@app.get("/products", response_model=list[Product])
def list_products():
    """List all products."""
    return []


@app.get("/products/{sku}", response_model=Product)
def get_product(sku: str):
    """Fetch a product by SKU."""
    raise HTTPException(status_code=404, detail="Unknown SKU")
