from app.extract.adapters.bigbasket import BigBasketAdapter
from app.extract.adapters.fatsecret import FatSecretAdapter
from app.extract.adapters.generic_table import (
    GenericTableAdapter,
    NutrabayAdapter,
    SwiggyAdapter,
    ZeptoAdapter,
)
from app.extract.adapters.shopify_jsonld import ShopifyJsonLdAdapter

__all__ = [
    "BigBasketAdapter",
    "FatSecretAdapter",
    "GenericTableAdapter",
    "NutrabayAdapter",
    "SwiggyAdapter",
    "ZeptoAdapter",
    "ShopifyJsonLdAdapter",
]
