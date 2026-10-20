"""Source-first valuation: JSON-LD offers, local-market checks and dated FX."""
import asyncio
import hashlib
import json
import math
import re
import time
from collections import OrderedDict
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .models import Book, Item, Locale, Price, utcnow
from .providers import GeminiProvider, fetch_public, ProviderUnavailable, public_url
from .vision import normalized

COUNTRIES = {"India":"IN", "United Kingdom":"GB", "United States":"US", "Canada":"CA", "Australia":"AU"}
REGIONAL_DOMAINS = {"IN": (".in", "flipkart.com", "bookchor.com"),
                    "GB": (".co.uk", ".uk"), "US": ("amazon.com", "barnesandnoble.com"),
                    "CA": (".ca",), "AU": (".com.au",)}


def can_price_record(record: Book | Item) -> bool:
    if record.excluded or record.status == "needs_appraisal":
        return False
    if isinstance(record, Book):
        return record.status == "identified" and not record.rare_signals
    if normalized(record.category) in {"", "object", "item", "unknown", "unidentified"}:
        return False
    return record.is_print or not any(word in normalized(record.category) for word in ["art", "portrait", "painting"])


def pricing_query(record: Book | Item) -> dict:
    if isinstance(record, Book):
        return {"kind":"book", "title":record.title, "author":record.author, "isbn":record.isbn}
    return {"kind":"item", "title":record.brand_model or (record.material+" "+record.category).strip(),
            "category":record.category, "material":record.material, "brand_model":record.brand_model,
            "dimensions_cm":record.dimensions_cm.model_dump(exclude_none=True)}


def pricing_fingerprint(record: Book | Item) -> str:
    """Price identity uses 2 cm bins; offer validation keeps the original values.

    This is bounded quantization, not hysteresis: a tiny change across a bin
    boundary can still invalidate a job. No category-specific sizing assumptions.
    """
    query=pricing_query(record)
    identity={key:normalized(value) if isinstance(value,str) else value for key,value in query.items()}
    if "isbn" in identity:
        identity["isbn"]=re.sub(r"[\s-]", "", query["isbn"]).casefold()
    if "dimensions_cm" in identity:
        identity["title"]=identity["brand_model"] or (identity["material"]+" "+identity["category"]).strip()
        identity["dimensions_cm"]={key:int(Decimal(str(value))/Decimal("2"))
                                   for key,value in query["dimensions_cm"].items()}
    return hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()


def source_label(value) -> str:
    if isinstance(value,list):
        return " ".join(source_label(v) for v in value)
    return str(value.get("name", "")) if isinstance(value,dict) else str(value or "")


def attribute_words(value) -> list[str]:
    """Handle inflected retailer labels without inventing category synonyms."""
    words=normalized(source_label(value).replace("_"," ")).split()
    return [w[:-3]+"y" if w.endswith("ies") else w[:-2] if w.endswith("sses")
            else w[:-1] if len(w)>3 and w.endswith("s") and not w.endswith("ss") else w for w in words]


def dimension_cm(quantity) -> float | None:
    units={"CMT":1,"CM":1,"CENTIMETER":1,"CENTIMETERS":1,"MTR":100,"M":100,
           "MMT":.1,"MM":.1,"INH":2.54,"IN":2.54,"INCH":2.54,"INCHES":2.54}
    if isinstance(quantity,dict):
        value=quantity.get("value")
        unit=str(quantity.get("unitCode") or quantity.get("unitText", "")).upper()
    elif isinstance(quantity,str):
        match=re.match(r'^\s*([\d.]+)\s*([a-zA-Z]+)',quantity)
        if not match: return None
        value,unit=match.group(1),match.group(2).upper()
    else:
        return None
    try:
        actual=float(value)*units[unit]
        return actual if math.isfinite(actual) and actual>0 else None
    except (ValueError,TypeError,KeyError):
        return None


def item_match(product: dict, query: dict) -> tuple[str, str] | None:
    """Compare source attributes generically; category tokens alone are not identity."""
    category=attribute_words(query.get("category", ""))
    name_words=attribute_words(product.get("name"))
    name=normalized(source_label(product.get("name")))
    if not category or not name:
        return None
    # Match the leaf of retailer taxonomies, not incidental words in parent paths.
    source_categories=product.get("category") or []
    source_categories=source_categories if isinstance(source_categories,list) else [source_categories]
    leaves=[attribute_words(re.split(r"[>/|]",source_label(c))[-1]) for c in source_categories]
    leaves=[leaf for leaf in leaves if leaf]
    if leaves:
        if not any(leaf[-1]==category[-1] and
                   (set(category)<=set(leaf) or (set(leaf)<=set(category) and set(category)<=set(name_words)))
                   for leaf in leaves):
            return None
    elif not set(category)<=set(name_words):
        return None
    # Generic product/accessory relations, independent of object categories.
    accessory_words={"cover","case","protector","sleeve","adapter","adaptor","mount","bracket",
                     "stand","holder","shade","accessory","kit","cap","lid","strap","charger",
                     "cable","battery","wheel","cushion","pad"}
    # "Chair with cushion" describes a chair; "chair cushion" and "cushion
    # for chair" describe the accessory. Inspect the primary noun phrase only.
    attachment=next((n for n,word in enumerate(name_words)
                     if word in {"with","including","includes","featuring","bundled"}),len(name_words))
    subject=name_words[:attachment]
    if (set(subject)&accessory_words)-set(category) or "compatible" in subject:
        return None
    observed_model=normalized(query.get("brand_model", ""))
    brand=normalized(source_label(product.get("brand")))
    model=normalized(source_label(product.get("model") or product.get("mpn")))
    if observed_model and brand and not set(brand.split())<=set(observed_model.split()):
        return None
    brand_only=bool(brand and observed_model==brand)
    exact=bool(observed_model and model and not brand_only and
               (any(c.isdigit() for c in model) or (brand and model!=brand)) and
               observed_model in {model, (brand+" "+model).strip()})
    unchecked=[]
    if observed_model and not exact:
        if model and not brand_only:
            return None
        if not set(observed_model.split())<=set(name.split()):
            return None
        unchecked.append("model identifier unverified")
    material=normalized(query.get("material", ""))
    checked=["retailer category" if leaves else "product-name category"]
    if material:
        wanted_material=set(attribute_words(material))-{ "and", "with" }
        source_material=set(attribute_words(product.get("material")))-{ "and", "with" }
        if source_material and not wanted_material<=source_material:
            return None
        if wanted_material<=source_material or wanted_material<=set(name_words):
            checked.append("material")
        else:
            unchecked.append("material unverified")
    for key,field in (("w","width"),("h","height"),("d","depth")):
        value=query.get("dimensions_cm",{}).get(key)
        if value is None:
            continue
        quantity=product.get(field)
        if quantity in (None,""):
            unchecked.append(field+" unverified")
            continue
        actual=dimension_cm(quantity)
        try:
            if actual is None or not math.isfinite(value) or value<=0 or abs(actual-value)/value > .15:
                return None
        except (ValueError,TypeError):
            return None
        checked.append(field)
    caveat=("; "+", ".join(unchecked)) if unchecked else ""
    if exact:
        return "exact", "Observed brand/model agrees with the retailer product identifier"+caveat
    return "equivalent", "Comparable retail product; matching "+", ".join(checked)+caveat+"; exact identity is unverified"


def schema_types(node) -> set[str]:
    types=node.get("@type",[]) or []
    types=[types] if isinstance(types,str) else types
    if not isinstance(types,list): return set()
    return {str(t).rstrip("/").split("/")[-1] for t in types}


def walk_products(node):
    if isinstance(node, list):
        for value in node:
            yield from walk_products(value)
    elif isinstance(node, dict):
        types=schema_types(node)
        if types & {"Product", "Book", "IndividualProduct"}:
            yield node
        if "ProductGroup" in types:
            inherited={k:node[k] for k in ("name","brand","category","material","description") if k in node}
            variants=node.get("hasVariant",[])
            variants=variants if isinstance(variants,list) else [variants]
            for variant in variants:
                if isinstance(variant,dict):
                    yield from walk_products(inherited|variant)
        for key in ("@graph", "mainEntity", "itemListElement", "item"):
            if key in node:
                yield from walk_products(node[key])


def local_market(offer: dict, url: str, locale: Locale) -> bool:
    code = COUNTRIES.get(locale.country, locale.country.upper())
    regions = offer.get("eligibleRegion", [])
    regions = regions if isinstance(regions, list) else [regions]
    for region in regions:
        value = region.get("name", region.get("addressCountry", "")) if isinstance(region, dict) else region
        if str(value).upper() in {code, locale.country.upper()}:
            return True
    if any(regions):
        return False
    shipping=offer.get("shippingDetails",[]) or []
    shipping=shipping if isinstance(shipping,list) else [shipping]
    destinations=[]
    for detail in shipping:
        if not isinstance(detail,dict): continue
        destination=detail.get("shippingDestination",[]) or []
        destinations.extend(destination if isinstance(destination,list) else [destination])
    countries=[source_label(d.get("addressCountry")) for d in destinations if isinstance(d,dict) and d.get("addressCountry")]
    if countries:
        return any(c.upper() in {code,locale.country.upper()} for c in countries)
    host = urlsplit(url).hostname or ""
    return any(host.endswith(suffix) if suffix.startswith(".") else host == suffix or host.endswith("."+suffix)
               for suffix in REGIONAL_DOMAINS.get(code, ()))


def walk_offers(node):
    if isinstance(node,list):
        for value in node: yield from walk_offers(value)
    elif isinstance(node,dict):
        if "Offer" in schema_types(node):
            yield node
        elif "AggregateOffer" in schema_types(node):
            # A price range is not an individually retrievable offer.
            yield from walk_offers(node.get("offers",[]))


def manufacturer_new_assumption(product,offer,url) -> bool:
    """Only a same-brand manufacturer catalogue can supply an absent-new assumption."""
    host=(urlsplit(url).hostname or "").lower().removeprefix("www.")
    brand="".join(normalized(source_label(product.get("brand"))).split())
    if not brand or host.split(".")[0]!=brand:
        return False
    if not re.search(r"/(?:products?|p)/[^/]+",urlsplit(url).path):
        return False
    offer_url=urljoin(url,str(offer.get("url") or ""))
    if urlsplit(offer_url).hostname!=urlsplit(url).hostname or urlsplit(offer_url).path!=urlsplit(url).path:
        return False
    text=normalized(" ".join(source_label(product.get(k)) for k in ("name","description","category")))
    return not re.search(r"\b(?:used|refurbished|renewed|damaged|pre owned|second hand|open box)\b",text)


def extract_offers(document: str, url: str, query: dict, locale: Locale, kind: str) -> list[tuple[Price, dict]]:
    results = []
    soup = BeautifulSoup(document, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        for product in walk_products(data):
            name = normalized(str(product.get("name", "")))
            if kind=="new" and re.search(r"\b(?:used|refurbished|renewed|damaged|pre owned|second hand|open box)\b",name):
                continue
            wanted = normalized(query.get("title", ""))
            matching=item_match(product,query) if query.get("kind")=="item" else None
            if query.get("kind")=="item" and matching is None:
                continue
            if not wanted or (query.get("kind")!="item" and not all(word in name.split() for word in wanted.split())):
                continue
            if query.get("kind","book")=="book" and not query.get("isbn") and name!=wanted:
                # Shared title words do not establish the same work (Dune vs Dune Messiah).
                continue
            authors = product.get("author", [])
            author_text = normalized(json.dumps(authors, ensure_ascii=False))
            author_words = [w for w in normalized(query.get("author", "")).split() if len(w)>1]
            if author_words and not all(w in (name+" "+author_text).split() for w in author_words):
                continue
            if query.get("isbn") and str(product.get("isbn", "")).replace("-", "") != query["isbn"].replace("-", ""):
                continue
            for offer in walk_offers(product.get("offers", [])):
                specification=offer.get("priceSpecification",{})
                specification=specification if isinstance(specification,dict) else {}
                if offer.get("price") is None and (specification.get("validForMemberTier") or specification.get("billingDuration")
                        or str(specification.get("priceType", "")).split("/")[-1] not in {"","SalePrice"}):
                    continue
                currency=offer.get("priceCurrency") or specification.get("priceCurrency", "")
                if str(currency).upper() != locale.currency or not local_market(offer, url, locale):
                    continue
                if str(offer.get("availability", "")).split("/")[-1] != "InStock":
                    continue
                condition = str(offer.get("itemCondition", product.get("itemCondition", ""))).split("/")[-1]
                condition_assumed="new" if kind=="new" else "used — seller grade unspecified"
                condition_evidence=condition
                if not condition and kind=="new" and manufacturer_new_assumption(product,offer,url):
                    condition_assumed="new replacement assumed; retailer condition unspecified"
                    condition_evidence="manufacturer catalogue; itemCondition absent"
                elif condition != ("NewCondition" if kind == "new" else "UsedCondition"):
                    continue
                try:
                    amount = Decimal(str(offer.get("price") if offer.get("price") is not None else specification.get("price", "")))
                    if not amount.is_finite() or amount <= 0:
                        continue
                except InvalidOperation:
                    continue
                if offer.get("priceValidUntil"):
                    try:
                        if date.fromisoformat(offer["priceValidUntil"][:10]) < date.today():
                            continue
                    except ValueError:
                        continue
                if matching:
                    match,basis=matching
                elif query.get("isbn"):
                    match,basis="exact", "Observed ISBN agrees with the retailer ISBN"
                else:
                    match,basis="equivalent", "Matching title and observed author; edition is unverified"
                offer_url=urljoin(url,str(offer.get("url") or ""))
                try:
                    public_url(offer_url,resolve=False)
                    if urlsplit(offer_url).hostname!=urlsplit(url).hostname: offer_url=url
                except ValueError:
                    offer_url=url
                evidence = {"query":query,"match":match,"match_basis":basis,
                            "product_attributes":{k:product[k] for k in ("category","brand","model","mpn","material","width","height","depth") if k in product},
                            "url":offer_url,"page_url":url, "retrieved_at":utcnow(), "product_name":product.get("name"),
                            "author":authors, "isbn":product.get("isbn"), "offer":offer,
                            "condition_evidence":condition_evidence,
                            "page_sha256":hashlib.sha256(document.encode()).hexdigest(),
                            "extraction":"JSON-LD Product/Book Offer; no model-supplied prices"}
                p = Price(amount=amount, currency=locale.currency, country=locale.country,
                          source=urlsplit(url).hostname or "", url=offer_url, retrieved_at=evidence["retrieved_at"],
                          condition_assumed=condition_assumed,
                          verified=True, exclusion_reason="", match=match,match_basis=basis,
                          product_name=str(product.get("name", "")))
                p.source_type="retailer_offer"
                results.append((p,evidence))
    return results


def convert_offer(price: Price, locale: Locale, fx: dict, ref: str) -> Price:
    if fx.get("base", "").upper() != price.currency or fx.get("quote", "").upper() != locale.currency or not fx.get("date"):
        raise ValueError("FX pair or date does not support this conversion")
    date.fromisoformat(fx["date"])
    rate = Decimal(str(fx["rate"]))
    if not rate.is_finite() or rate <= 0 or price.amount is None or not ref:
        raise ValueError("Invalid FX rate/source")
    return price.model_copy(update={"amount":(price.amount*rate).quantize(Decimal(".01"), rounding=ROUND_HALF_UP),
                                    "currency":locale.currency, "country":locale.country, "converted":True,
                                    "original_amount":price.amount, "original_currency":price.currency,
                                    "fx_rate":rate, "fx_source_ref":ref})


class PriceProvider:
    def __init__(self, provider: GeminiProvider, threshold_inr=Decimal("10000")):
        self.provider = provider
        self.threshold_inr = threshold_inr
        self.cache = OrderedDict()
        self.inflight = {}
        self.search_cache = OrderedDict()
        self.search_inflight = {}
        self.fx_cache = {}
        self.limit = asyncio.Semaphore(4)

    async def fx(self, base: str, quote: str) -> dict:
        key = (base,quote)
        if key not in self.fx_cache:
            url = f"https://api.frankfurter.dev/v2/rate/{base.lower()}/{quote.lower()}"
            _, text = await fetch_public(url, max_bytes=20_000)
            self.fx_cache[key] = {**json.loads(text), "url":url, "retrieved_at":utcnow()}
        return self.fx_cache[key]

    async def search(self,text):
        cached=self.search_cache.get(text)
        if cached and time.monotonic()-cached[0]<900:
            self.search_cache.move_to_end(text)
            return list(cached[1])
        if text in self.search_inflight:
            return list(await asyncio.shield(self.search_inflight[text]))
        task=asyncio.create_task(self.provider.search_urls(text))
        self.search_inflight[text]=task
        try:
            urls=await task
            self.search_cache[text]=(time.monotonic(),list(urls))
            self.search_cache.move_to_end(text)
            while len(self.search_cache)>256:
                self.search_cache.popitem(last=False)
            return urls
        finally:
            self.search_inflight.pop(text,None)

    async def local(self, query: dict, locale: Locale, kind: str, *, attempts=None):
        # Dimensions affect validation, not discovery. Small measurement changes
        # can reuse search URLs while offers are checked against each actual query.
        terms=dict.fromkeys(str(query.get(k) or "").strip() for k in ("title","brand_model","category","material","author","isbn"))
        text = ' '.join(t for t in terms if t)+f' {kind} buy in {locale.country} {locale.currency} current {date.today().isoformat()}'
        urls = await self.search(text)
        attempt={"query":text,"country":locale.country,"currency":locale.currency,"kind":kind,"results":[]}
        if attempts is not None: attempts.append(attempt)
        async def retrieve(url):
            try:
                final_url, document = await fetch_public(url)
                offers=extract_offers(document, final_url, query, locale, kind)
                attempt["results"].append({"url":url,"page_url":final_url,
                                           "status":"supported_offer" if offers else "no_supported_offer",
                                           "offer_count":len(offers)})
                return offers
            except (ValueError, OSError, httpx_error()) as error:
                attempt["results"].append({"url":url,"status":"retrieval_failed","error_type":type(error).__name__})
                return []
        batches=await asyncio.gather(*(retrieve(url) for url in list(dict.fromkeys(urls))[:8]))
        candidates=[candidate for batch in batches for candidate in batch]
        return min(candidates, key=lambda pair: (pair[0].match!="exact",
                   pair[1]["condition_evidence"] not in {"NewCondition","UsedCondition"},pair[0].amount)) if candidates else None

    async def quote(self, query: dict, locale: Locale, kind: str) -> tuple[Price, list[tuple[str, dict]]]:
        key = (json.dumps(query, sort_keys=True), locale.country, locale.currency, kind)
        cached=self.cache.get(key)
        if cached and time.monotonic()-cached[0] < 900:
            self.cache.move_to_end(key)
            return deepcopy(cached[1])
        if key in self.inflight:
            return deepcopy(await asyncio.shield(self.inflight[key]))
        future=asyncio.get_running_loop().create_future()
        self.inflight[key]=future
        try:
            async with self.limit:
                async with asyncio.timeout(45):
                    result=await self._quote(query,locale,kind)
        except asyncio.CancelledError:
            result=(Price(exclusion_reason="Valuation interrupted; no automatic retry"),[])
            self._cache_result(key,result)
            future.set_result(result)
            raise
        except TimeoutError:
            result=(Price(exclusion_reason="Valuation deadline reached; no automatic retry"),[])
        finally:
            self.inflight.pop(key,None)
        self._cache_result(key,result)
        future.set_result(result)
        return deepcopy(result)

    def _cache_result(self,key,result):
        self.cache[key]=(time.monotonic(),deepcopy(result))
        self.cache.move_to_end(key)
        while len(self.cache)>256:
            self.cache.popitem(last=False)

    async def _quote(self,query,locale,kind):
        sources = []
        attempts=[]
        try:
            result = await self.local(query, locale, kind, attempts=attempts)
            foreign = False
            if result is None:
                fallback = Locale(country="United Kingdom", currency="GBP") if locale.currency != "GBP" else Locale(country="India", currency="INR")
                result = await self.local(query, fallback, kind, attempts=attempts)
                foreign = result is not None
            trace={"retrieved_at":utcnow(),"query":query,"attempts":attempts,
                   "extraction":"Search URLs only; prices require retrieved structured product offers"}
            trace_ref="sources/search_"+hashlib.sha256(json.dumps(trace,sort_keys=True).encode()).hexdigest()[:20]+".json"
            sources.append((trace_ref,trace))
            if result is None:
                return Price(exclusion_reason="No retrievable in-stock offer with supported product, condition and market"), sources
            p, evidence = result
            evidence["search_ref"]=trace_ref
            ref = "sources/offer_" + hashlib.sha256(json.dumps(evidence,sort_keys=True).encode()).hexdigest()[:20] + ".json"
            p.source_ref = ref
            sources.append((ref,evidence))
            if foreign:
                fx = await self.fx(p.currency, locale.currency)
                fx_ref = "sources/fx_"+hashlib.sha256(json.dumps(fx,sort_keys=True).encode()).hexdigest()[:20]+".json"
                sources.append((fx_ref, fx))
                p = convert_offer(p, locale, fx, fx_ref)
            inr_amount = p.amount
            if p.currency != "INR":
                fx = await self.fx(p.currency, "INR")
                fx_ref = "sources/threshold_fx_"+hashlib.sha256(json.dumps(fx,sort_keys=True).encode()).hexdigest()[:20]+".json"
                sources.append((fx_ref, fx))
                inr_amount = convert_offer(p, Locale(country="India",currency="INR"), fx, fx_ref).amount
            if inr_amount > self.threshold_inr:
                return Price(exclusion_reason="Retrieved offer exceeds appraisal threshold; human appraisal required"), sources
            return p, sources
        except ProviderUnavailable as error:
            return Price(exclusion_reason=str(error)), sources
        except Exception as error:
            return Price(exclusion_reason=f"Price/FX retrieval failed ({type(error).__name__}); no estimate substituted"), sources


def httpx_error():
    # Import here keeps the parser usable independently of the HTTP adapter.
    import httpx
    return httpx.HTTPError
