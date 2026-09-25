import Foundation
import XCTest
import WavrModels

final class SnapshotTests: XCTestCase {
    func testEveryCanonicalSnapshot() throws {
        // Resolve from this source file so SwiftPM can run without bundling a
        // second, potentially stale copy of the generated conformance fixture.
        let fixtureURL = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../conformance/client_view.json")
            .standardizedFileURL
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: fixtureURL)) as? [String: Any])
        let cases = try XCTUnwrap(root["cases"] as? [[String: Any]])
        XCTAssertEqual(cases.count, 12)
        for fixture in cases {
            let name = try XCTUnwrap(fixture["name"] as? String)
            let expected = try XCTUnwrap(fixture["snapshot"] as? [String: Any])
            let data = try JSONSerialization.data(withJSONObject: expected)
            let snapshot = try JSONDecoder().decode(Snapshot.self, from: data)

            XCTAssertEqual(snapshot.schema, 1, name)
            XCTAssertEqual(snapshot.exitCode, expected["exit_code"] as? Int, name)
            XCTAssertEqual(snapshot.reachable, expected["reachable"] as? Bool, name)
            XCTAssertEqual(snapshot.error, expected["error"] as? String, name)
            XCTAssertEqual(snapshot.roomsReadable, expected["rooms_readable"] as? Bool, name)
            let privacy = try XCTUnwrap(expected["privacy"] as? [String: Any], name)
            XCTAssertEqual(snapshot.privacy?.watch, privacy["watch"] as? Bool, name)

            let runtime = expected["runtime"] as? [String: Any]
            XCTAssertEqual(snapshot.runtime?.state?.rawValue, runtime?["state"] as? String, name)
            XCTAssertEqual(snapshot.runtime?.headline, runtime?["headline"] as? String, name)
            XCTAssertEqual(snapshot.runtime?.space, runtime?["space"] as? String, name)
            XCTAssertEqual(snapshot.runtime?.role, runtime?["role"] as? String, name)
            XCTAssertEqual(snapshot.runtime?.uptimeSeconds, runtime?["uptime_s"] as? Double, name)
            XCTAssertEqual(snapshot.runtime?.lastStateAgeSeconds, runtime?["last_state_age_s"] as? Double, name)
            let findings = runtime?["findings"] as? [[String: Any]]
            XCTAssertEqual(snapshot.runtime?.findings?.count, findings?.count, name)
            for (actual, item) in zip(snapshot.runtime?.findings ?? [], findings ?? []) {
                XCTAssertEqual(actual.key, item["key"] as? String, name)
                XCTAssertEqual(actual.state?.rawValue, item["state"] as? String, name)
                XCTAssertEqual(actual.text, item["text"] as? String, name)
                XCTAssertEqual(actual.detail, item["detail"] as? String, name)
            }

            let attention = expected["attention"] as? [String: Any]
            XCTAssertEqual(snapshot.attention?.total, attention?["total"] as? Int, name)
            XCTAssertEqual(snapshot.attention?.blocking, attention?["blocking"] as? Int, name)
            XCTAssertEqual(snapshot.attention?.degraded, attention?["degraded"] as? Int, name)
            XCTAssertEqual(snapshot.attention?.info, attention?["info"] as? Int, name)
            XCTAssertEqual(snapshot.attention?.headline, attention?["headline"] as? String, name)
            let checks = attention?["could_not_check"] as? [Any]
            XCTAssertEqual(snapshot.attention?.couldNotCheck?.count, checks?.count, name)
            for (actual, item) in zip(snapshot.attention?.couldNotCheck ?? [], checks ?? []) {
                XCTAssertEqual(actual, item as? String, name)
            }
            let items = attention?["items"] as? [[String: Any]]
            XCTAssertEqual(snapshot.attention?.items?.count, items?.count, name)
            for (actual, item) in zip(snapshot.attention?.items ?? [], items ?? []) {
                XCTAssertEqual(actual.key, item["key"] as? String, name)
                XCTAssertEqual(actual.band, item["band"] as? String, name)
                XCTAssertEqual(actual.title, item["title"] as? String, name)
                XCTAssertEqual(actual.detail, item["detail"] as? String, name)
                XCTAssertEqual(actual.location, item["where"] as? String, name)
                XCTAssertEqual(actual.action, item["action"] as? String, name)
                XCTAssertEqual(actual.since, item["since"] as? String, name)
                XCTAssertEqual(actual.count, item["count"] as? Int, name)
            }

            let rooms = try XCTUnwrap(expected["rooms"] as? [[String: Any]], name)
            XCTAssertEqual(snapshot.rooms?.count, rooms.count, name)
            for (actual, room) in zip(snapshot.rooms ?? [], rooms) {
                XCTAssertEqual(actual.room, room["room"] as? String, name)
                XCTAssertEqual(actual.occupied, room["occupied"] as? Bool, name)
                XCTAssertEqual(actual.confidence, room["confidence"] as? Double, name)
                XCTAssertEqual(actual.personCount, room["person_count"] as? Int, name)
                XCTAssertEqual(actual.precisionLevel, room["precision_level"] as? String, name)
                XCTAssertEqual(actual.explanation, room["explanation"] as? String, name)
                XCTAssertEqual(actual.timestamp, room["ts"] as? String, name)
                XCTAssertEqual(actual.watch, room["watch"] as? Bool, name)
                XCTAssertEqual(actual.unrecognized, room["unrecognized"] as? Bool, name)
                let sources = try XCTUnwrap(room["sources"] as? [[String: Any]], name)
                XCTAssertEqual(actual.sources?.count, sources.count, name)
                for (source, value) in zip(actual.sources ?? [], sources) {
                    XCTAssertEqual(source.modality, value["modality"] as? String, name)
                    XCTAssertEqual(source.sensorID, value["sensor_id"] as? String, name)
                    XCTAssertEqual(source.presence, value["presence"] as? Bool, name)
                    XCTAssertEqual(source.confidence, value["confidence"] as? Double, name)
                    XCTAssertEqual(source.ageSeconds, value["age_s"] as? Double, name)
                    XCTAssertEqual(source.health, value["health"] as? String, name)
                    XCTAssertEqual(source.count, value["count"] as? Int, name)
                }
            }
        }
    }

    func testHealthySample() throws {
        let json = #"{"schema":1,"reachable":true,"error":null,"exit_code":1,"runtime":{"state":"healthy","headline":"Wavr is running","space":"Home","role":"core","uptime_s":3600,"last_state_age_s":4.5,"findings":[{"key":"sensors","state":"healthy","text":"3 sensors","detail":null}]},"attention":{"total":1,"blocking":0,"degraded":1,"info":0,"headline":"1 thing needs your attention","could_not_check":[],"items":[]},"rooms":[{"room":"office","occupied":true,"confidence":0.87,"person_count":1,"precision_level":"position","explanation":"mmwave: presence","ts":"2026-09-24T10:00:00+00:00","watch":false,"unrecognized":false,"sources":[]}],"rooms_readable":true,"privacy":{"watch":false}}"#
        let snapshot = try JSONDecoder().decode(Snapshot.self, from: Data(json.utf8))
        XCTAssertEqual(snapshot.schema, 1)
        XCTAssertEqual(snapshot.runtime?.state, .healthy)
        XCTAssertEqual(snapshot.runtime?.findings?.first?.text, "3 sensors")
        XCTAssertEqual(snapshot.rooms?.first?.confidence, 0.87)
        XCTAssertEqual(snapshot.attention?.total, 1)
    }

    func testUnreachableSample() throws {
        let json = #"{"schema":1,"reachable":false,"error":"cannot connect","exit_code":2,"runtime":null,"attention":null,"rooms":[],"rooms_readable":false,"privacy":{"watch":false}}"#
        let snapshot = try JSONDecoder().decode(Snapshot.self, from: Data(json.utf8))
        XCTAssertEqual(snapshot.reachable, false)
        XCTAssertNil(snapshot.runtime)
        XCTAssertNil(snapshot.attention)
        XCTAssertEqual(snapshot.rooms?.count, 0)
        XCTAssertEqual(snapshot.error, "cannot connect")
    }

    func testWrongTypesRemainUnknownWithoutDroppingSnapshot() throws {
        let json = #"{"schema":"broken","reachable":true,"runtime":{"state":"future-state","headline":42,"space":"Home","findings":[{"key":"ok","text":false}]},"attention":{"total":"bad","items":[{"title":"Read me","count":"bad"}]},"rooms":[{"room":"office","occupied":"yes","confidence":"high","sources":[{"modality":"ble","age_s":"old"}]}],"rooms_readable":"yes","privacy":{"watch":"false"}}"#
        let snapshot = try JSONDecoder().decode(Snapshot.self, from: Data(json.utf8))
        XCTAssertNil(snapshot.schema)
        XCTAssertEqual(snapshot.reachable, true)
        XCTAssertEqual(snapshot.runtime?.state, .unknown)
        XCTAssertNil(snapshot.runtime?.headline)
        XCTAssertEqual(snapshot.runtime?.space, "Home")
        XCTAssertNil(snapshot.runtime?.findings?.first?.text)
        XCTAssertNil(snapshot.attention?.total)
        XCTAssertEqual(snapshot.attention?.items?.first?.title, "Read me")
        XCTAssertNil(snapshot.attention?.items?.first?.count)
        XCTAssertNil(snapshot.rooms?.first?.occupied)
        XCTAssertNil(snapshot.rooms?.first?.confidence)
        XCTAssertNil(snapshot.rooms?.first?.sources?.first?.ageSeconds)
        XCTAssertNil(snapshot.roomsReadable)
        XCTAssertNil(snapshot.privacy?.watch)
    }
}
