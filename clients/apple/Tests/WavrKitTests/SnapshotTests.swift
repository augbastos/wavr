import Foundation
import XCTest
import WavrModels

final class SnapshotTests: XCTestCase {
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
