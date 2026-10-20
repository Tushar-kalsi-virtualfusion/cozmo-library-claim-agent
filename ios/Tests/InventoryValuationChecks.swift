import Foundation

@main struct InventoryValuationChecks {
    static func json(_ text: String) throws -> JSONValue {
        try JSONDecoder().decode(JSONValue.self,from:Data(text.utf8))
    }
    static func offer(_ changes: [String:JSONValue] = [:]) -> JSONValue {
        var value: [String:JSONValue] = ["amount":.string("1234.50"),"currency":.string("INR"),
            "verified":.bool(true),"source":.string("Source shop"),"url":.string("https://shop.example/product"),
            "source_ref":.string("sources/price.json"),"retrieved_at":.string("2026-10-20T10:00:00+00:00"),
            "match":.string("equivalent"),"match_basis":.string("Same category and material"),"product_name":.string("Comparable lamp")]
        value.merge(changes) { _,new in new }
        return .object(value)
    }
    static func main() throws {
        let quote = InventoryPriceEstimate(offer())!
        precondition(quote.moneyText == "INR 1,234.50")
        precondition(quote.match == .equivalent && quote.productName == "Comparable lamp")
        precondition(quote.matchBasis == "Same category and material")
        precondition(quote.sourceURL.absoluteString == "https://shop.example/product")
        precondition(!quote.retrievedDateText.isEmpty)
        precondition(InventoryPriceEstimate(offer(["amount":.string("0")]))?.moneyText == "INR 0.00")
        precondition(InventoryPriceEstimate(offer(["amount":.number(12.5)]))?.moneyText == "INR 12.50")
        precondition(InventoryPriceEstimate(offer(["amount":.string("1.25e3")]))?.moneyText == "INR 1,250.00")
        let range = offer(["amount":.null,"low":.string("1250.25"),"high":.string("2000.50")])
        precondition(InventoryPriceEstimate(range)?.moneyText == "INR 1,250.25–2,000.50")
        let single = offer(["amount":.null,"low":.string("1250"),"high":.string("1250")])
        precondition(InventoryPriceEstimate(single)?.moneyText == "INR 1,250.00")
        for bad: JSONValue in [.null,.bool(false),.string("NaN"),.string("Infinity"),.string("-1"),.string("12 dollars"),.string("1,000"),.number(.infinity)] {
            precondition(InventoryPriceEstimate(offer(["amount":bad])) == nil, "Malformed or absent money must stay unknown")
        }
        for invalid in [offer(["verified":.bool(false)]),offer(["url":.string("javascript:alert(1)")]),
                        offer(["url":.string("https://user:password@shop.example")]),offer(["retrieved_at":.string("yesterday")]),
                        offer(["source_ref":.string("")]),offer(["currency":.string("")]),
                        offer(["amount":.null,"low":.string("20"),"high":.string("10")]),
                        offer(["amount":.null,"low":.string("20"),"high":.null])] {
            precondition(InventoryPriceEstimate(invalid) == nil)
        }
        precondition(InventoryPriceEstimate(offer(["match":.string("exact")]))?.match == .exact)
        precondition(InventoryPriceEstimate(offer(["match":.string("unknown")]))?.match == .unknown)
        var value: [String:JSONValue] = ["id":.string("b1"),"title":.string("Dune"),"status":.string("identified"),
            "pricing_status":.string("searching"),"pricing_reason":.string("Used offer still searching"),
            "replacement_cost":offer(),"used_value":.object([:])]
        let current = InventoryRow(value:.object(value))
        precondition(current.pricingStatus == .searching && current.replacementEstimate != nil)
        precondition(current.resaleEstimate == nil && current.pricingReason == "Used offer still searching")
        value["excluded"] = .bool(true)
        precondition(InventoryRow(value:.object(value)).replacementEstimate == nil)
        value["excluded"] = .bool(false); value["status"] = .string("needs_appraisal")
        precondition(InventoryRow(value:.object(value)).replacementEstimate == nil)
        let selected = InventorySelection(id:"b1")
        let pending = InventoryRow(value:try json(#"{"id":"b1","pricing_status":"queued","replacement_cost":null}"#))
        precondition(selected.resolve(in:[pending])?.replacementEstimate == nil)
        precondition(selected.resolve(in:[current])?.replacementEstimate?.moneyText == "INR 1,234.50")
        precondition(selected.resolve(in:[]) == nil, "Removed items must not display a stale saved snapshot")
        print("Verified Decimal amounts/ranges, invalid evidence, async valuation and live item selection checks passed")
    }
}
