from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from store.routes import orders, pets

app = FastAPI(title="Pet Store API", version="1.2.0", description="Reference pet store service.")


class OutOfStockError(Exception):
    pass


@app.exception_handler(OutOfStockError)
def out_of_stock_handler(request: Request, exc: OutOfStockError):
    return JSONResponse(status_code=409, content={"detail": "out of stock"})


app.include_router(pets.router, prefix="/api/v1")
app.include_router(orders.router, prefix="/api/v1")


@app.get("/health", tags=["ops"])
def health() -> dict:
    """Liveness probe."""
    return {"status": "ok"}
