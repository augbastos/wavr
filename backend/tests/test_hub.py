from wavr.hub import Hub


async def test_publish_fans_out_to_all_subscribers():
    hub = Hub()
    a, b = hub.subscribe(), hub.subscribe()
    await hub.publish({"room": "sala"})
    assert (await a.get())["room"] == "sala"
    assert (await b.get())["room"] == "sala"


async def test_unsubscribe_stops_delivery():
    hub = Hub()
    a = hub.subscribe()
    hub.unsubscribe(a)
    await hub.publish({"room": "quarto"})
    assert a.empty()
