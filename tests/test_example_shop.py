from examples.shop.orders import add_order, connect, orders_for


def test_orders_roundtrip():
    conn = connect()
    oid = add_order(conn, "ada", 12.5)
    assert orders_for(conn, "ada") == [(oid, 12.5)]
