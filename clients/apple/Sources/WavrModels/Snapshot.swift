import Foundation

// An invalid element is unknown; it must not erase valid siblings in a Core array.
private struct KnownObjects<Element: Decodable>: Decodable {
    let values: [Element]

    init(from decoder: Decoder) throws {
        var array = try decoder.unkeyedContainer()
        var values: [Element] = []
        while !array.isAtEnd {
            let element = try array.superDecoder()
            if let value = try? Element(from: element) { values.append(value) }
        }
        self.values = values
    }
}

private struct NullableStrings: Decodable {
    let values: [String?]

    init(from decoder: Decoder) throws {
        var array = try decoder.unkeyedContainer()
        var values: [String?] = []
        while !array.isAtEnd {
            let element = try array.superDecoder()
            values.append(try? element.singleValueContainer().decode(String.self))
        }
        self.values = values
    }
}

public enum WavrState: String, Codable, Equatable, Sendable {
    case healthy, starting, updating, paused, degraded, attention, unavailable
    case unknown

    public init(from decoder: Decoder) throws {
        let value = try? decoder.singleValueContainer().decode(String.self)
        self = value.flatMap(Self.init(rawValue:)) ?? .unknown
    }
}

public struct Snapshot: Codable, Sendable {
    public let schema: Int?
    public let reachable: Bool?
    public let error: String?
    public let exitCode: Int?
    public let runtime: Runtime?
    public let attention: Attention?
    public let rooms: [Room]?
    public let roomsReadable: Bool?
    public let privacy: Privacy?

    private enum CodingKeys: String, CodingKey {
        case schema, reachable, error, runtime, attention, rooms, privacy
        case exitCode = "exit_code", roomsReadable = "rooms_readable"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        schema = try? c.decodeIfPresent(Int.self, forKey: .schema)
        reachable = try? c.decodeIfPresent(Bool.self, forKey: .reachable)
        error = try? c.decodeIfPresent(String.self, forKey: .error)
        exitCode = try? c.decodeIfPresent(Int.self, forKey: .exitCode)
        runtime = try? c.decodeIfPresent(Runtime.self, forKey: .runtime)
        attention = try? c.decodeIfPresent(Attention.self, forKey: .attention)
        rooms = (try? c.decodeIfPresent(KnownObjects<Room>.self, forKey: .rooms))?.values
        roomsReadable = try? c.decodeIfPresent(Bool.self, forKey: .roomsReadable)
        privacy = try? c.decodeIfPresent(Privacy.self, forKey: .privacy)
    }
}

public struct Runtime: Codable, Sendable {
    public let state: WavrState?
    public let headline: String?
    public let space: String?
    public let role: String?
    public let uptimeSeconds: Double?
    public let lastStateAgeSeconds: Double?
    public let findings: [Finding]?

    private enum CodingKeys: String, CodingKey {
        case state, headline, space, role, findings
        case uptimeSeconds = "uptime_s", lastStateAgeSeconds = "last_state_age_s"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        state = try? c.decodeIfPresent(WavrState.self, forKey: .state)
        headline = try? c.decodeIfPresent(String.self, forKey: .headline)
        space = try? c.decodeIfPresent(String.self, forKey: .space)
        role = try? c.decodeIfPresent(String.self, forKey: .role)
        uptimeSeconds = try? c.decodeIfPresent(Double.self, forKey: .uptimeSeconds)
        lastStateAgeSeconds = try? c.decodeIfPresent(Double.self, forKey: .lastStateAgeSeconds)
        findings = (try? c.decodeIfPresent(KnownObjects<Finding>.self, forKey: .findings))?.values
    }
}

public struct Finding: Codable, Sendable {
    public let key: String?
    public let state: WavrState?
    public let text: String?
    public let detail: String?

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        key = try? c.decodeIfPresent(String.self, forKey: .key)
        state = try? c.decodeIfPresent(WavrState.self, forKey: .state)
        text = try? c.decodeIfPresent(String.self, forKey: .text)
        detail = try? c.decodeIfPresent(String.self, forKey: .detail)
    }
}

public struct Attention: Codable, Sendable {
    public let total: Int?
    public let blocking: Int?
    public let degraded: Int?
    public let info: Int?
    public let headline: String?
    public let couldNotCheck: [String?]?
    public let items: [AttentionItem]?

    private enum CodingKeys: String, CodingKey {
        case total, blocking, degraded, info, headline, items
        case couldNotCheck = "could_not_check"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        total = try? c.decodeIfPresent(Int.self, forKey: .total)
        blocking = try? c.decodeIfPresent(Int.self, forKey: .blocking)
        degraded = try? c.decodeIfPresent(Int.self, forKey: .degraded)
        info = try? c.decodeIfPresent(Int.self, forKey: .info)
        headline = try? c.decodeIfPresent(String.self, forKey: .headline)
        couldNotCheck = (try? c.decodeIfPresent(NullableStrings.self, forKey: .couldNotCheck))?.values
        items = (try? c.decodeIfPresent(KnownObjects<AttentionItem>.self, forKey: .items))?.values
    }
}

public struct AttentionItem: Codable, Sendable {
    public let key: String?
    public let band: String?
    public let title: String?
    public let detail: String?
    public let location: String?
    public let action: String?
    public let since: String?
    public let count: Int?

    private enum CodingKeys: String, CodingKey {
        case key, band, title, detail, action, since, count
        case location = "where"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        key = try? c.decodeIfPresent(String.self, forKey: .key)
        band = try? c.decodeIfPresent(String.self, forKey: .band)
        title = try? c.decodeIfPresent(String.self, forKey: .title)
        detail = try? c.decodeIfPresent(String.self, forKey: .detail)
        location = try? c.decodeIfPresent(String.self, forKey: .location)
        action = try? c.decodeIfPresent(String.self, forKey: .action)
        since = try? c.decodeIfPresent(String.self, forKey: .since)
        count = try? c.decodeIfPresent(Int.self, forKey: .count)
    }
}

public struct Room: Codable, Sendable {
    public let room: String?
    public let occupied: Bool?
    public let confidence: Double?
    public let personCount: Int?
    public let precisionLevel: String?
    public let explanation: String?
    public let timestamp: String?
    public let watch: Bool?
    public let unrecognized: Bool?
    public let sources: [Source]?

    private enum CodingKeys: String, CodingKey {
        case room, occupied, confidence, explanation, watch, unrecognized, sources
        case personCount = "person_count", precisionLevel = "precision_level", timestamp = "ts"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        room = try? c.decodeIfPresent(String.self, forKey: .room)
        occupied = try? c.decodeIfPresent(Bool.self, forKey: .occupied)
        confidence = try? c.decodeIfPresent(Double.self, forKey: .confidence)
        personCount = try? c.decodeIfPresent(Int.self, forKey: .personCount)
        precisionLevel = try? c.decodeIfPresent(String.self, forKey: .precisionLevel)
        explanation = try? c.decodeIfPresent(String.self, forKey: .explanation)
        timestamp = try? c.decodeIfPresent(String.self, forKey: .timestamp)
        watch = try? c.decodeIfPresent(Bool.self, forKey: .watch)
        unrecognized = try? c.decodeIfPresent(Bool.self, forKey: .unrecognized)
        sources = (try? c.decodeIfPresent(KnownObjects<Source>.self, forKey: .sources))?.values
    }
}

public struct Source: Codable, Sendable {
    public let modality: String?
    public let sensorID: String?
    public let presence: Bool?
    public let confidence: Double?
    public let ageSeconds: Double?
    public let health: String?
    public let count: Int?

    private enum CodingKeys: String, CodingKey {
        case modality, presence, confidence, health, count
        case sensorID = "sensor_id", ageSeconds = "age_s"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        modality = try? c.decodeIfPresent(String.self, forKey: .modality)
        sensorID = try? c.decodeIfPresent(String.self, forKey: .sensorID)
        presence = try? c.decodeIfPresent(Bool.self, forKey: .presence)
        confidence = try? c.decodeIfPresent(Double.self, forKey: .confidence)
        ageSeconds = try? c.decodeIfPresent(Double.self, forKey: .ageSeconds)
        health = try? c.decodeIfPresent(String.self, forKey: .health)
        count = try? c.decodeIfPresent(Int.self, forKey: .count)
    }
}

public struct Privacy: Codable, Sendable {
    public let watch: Bool?

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        watch = try? c.decodeIfPresent(Bool.self, forKey: .watch)
    }
}
