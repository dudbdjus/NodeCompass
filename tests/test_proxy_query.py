from proxy_manager.services.proxy_query import ProxyQuery
from proxy_manager.storage.models import Proxy


def test_case_insensitive_search_and_combined_filters():
    one = Proxy(address="192.0.2.1", country="USA", status="Active", order_id="ORD-1", port_http=8081)
    two = Proxy(address="192.0.2.2", country="DEU", status="Active", order_id="ORD-2", port_http=8082)
    three = Proxy(address="192.0.2.3", country="USA", status="Ending")
    rows = [one, two, three]
    assert ProxyQuery.filter(rows, "ord-", "USA", "Active") == [one]
    assert ProxyQuery.filter(rows, "8082", "", "") == [two]
    assert ProxyQuery.filter(rows, "  ", "USA", "Ending") == [three]
