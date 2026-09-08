/**
 * TypeScript declarations for the Wavr Spatial SDK.
 *
 * Hand-written next to the JavaScript rather than generated from it, because
 * the JavaScript is the artefact that ships: the frontend this SDK belongs to
 * is zero-build on purpose, and adding a compile step to get types would mean
 * the file a developer reads is no longer the file that runs.
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

export const PROTOCOL_VERSION: number;

export const ERROR_NETWORK: "network";
export const ERROR_AUTH: "auth";
export const ERROR_NOT_FOUND: "not_found";
export const ERROR_PROTOCOL: "protocol";
export const ERROR_SERVER: "server";

export type WavrErrorKind =
  | typeof ERROR_NETWORK
  | typeof ERROR_AUTH
  | typeof ERROR_NOT_FOUND
  | typeof ERROR_PROTOCOL
  | typeof ERROR_SERVER;

export class WavrError extends Error {
  kind: WavrErrorKind;
  status: number;
  cause: unknown;
  /** Whether retrying the same call could plausibly succeed. */
  readonly retryable: boolean;
}

/** What an application may ask a room for. */
export type Capability =
  | "presence"
  | "count"
  | "position"
  | "anchors"
  | "display"
  | "audio";

/** How detailed an answer a room can support. Orthogonal to confidence. */
export type Precision = "none" | "house" | "room" | "count" | "position";

export interface Anchor {
  anchor_id: string;
  name: string;
  room: string;
  kind: "logical" | "point" | "area";
  positioned: boolean;
  /** Present only on a positioned anchor, always in the `room` frame. */
  x?: number;
  y?: number;
  z?: number;
  frame?: "room";
  polygon?: Array<[number, number]>;
  /** True when the room this anchor names no longer exists. */
  orphaned?: boolean;
  mappings?: Array<{
    provider_id: string;
    external_id: string;
    origin: "declared" | "imported";
  }>;
}

export interface ContextDevice {
  device_id: string;
  /**
   * Derived from what the device can DO and where it is — "living room screen
   * 2" — never the pairing name, which is typed by whoever paired it and is
   * very often a person's. Enough to ask "Continue on which?", and it tells an
   * experience nothing about whose phone it is.
   *
   * This was declared as `name`, which the Core has never served: the field is
   * `label` in `experience._visible_devices`. TypeScript therefore promised a
   * string on every device and delivered `undefined` on all of them, and the
   * capability-aware reference page rendered "on undefined".
   */
  label: string;
  functions: string[];
  /** Tristate. `null` means the device never said — which is NOT "no screen". */
  display: boolean | null;
  audio: boolean | null;
  uwb: boolean | null;
}

/** Where a piece of evidence comes from, without identifying the equipment. */
export type SensorSource = "wavr" | "home_assistant" | "external" | "simulated";

export interface ContextSensor {
  /**
   * Derived, never the name an operator typed: "camera 1", "mmwave 2". Stable
   * within one response, which is enough to correlate two readings, and it
   * tells you nothing about whose room this is. (Protocol 2 removed
   * `sensor_id`, which carried the camera's or node's own name.)
   */
  label: string;
  modality: string;
  health: "ok" | "offline" | "disabled" | "unknown";
  precision_level: Precision;
  /** Where this evidence comes from, without identifying the equipment. */
  source: SensorSource;
  /**
   * A boolean rather than a prefix you have to remember to parse. Simulated
   * evidence is never indistinguishable from real evidence, and a guarantee a
   * client can forget to check is not a guarantee.
   */
  simulated: boolean;
}

export class RoomContext {
  raw: Record<string, unknown>;
  room: string;
  precision: Precision;
  confidence: number;
  /** `null` when nothing here reported. */
  occupied: boolean | null;
  /** `null` when nothing here can count. Never 0 for "unknown". */
  occupancy: number | null;
  occupancyKnown: boolean;
  capabilities: Capability[];
  anchors: Anchor[];
  devices: ContextDevice[];
  sensors: ContextSensor[];
  /** Plain sentences naming what this room cannot answer right now. */
  limitations: string[];
  can(capability: Capability): boolean;
  anchor(): Anchor[];
  anchor(name: string): Anchor | null;
  displays(): ContextDevice[];
}

export interface SpatialEvent {
  event:
    | "room.occupancy_changed"
    | "room.count_changed"
    | "room.precision_changed"
    | "room.sensors_disagree"
    | "room.sensors_agree"
    | "sensor.offline"
    | "sensor.online";
  room: string;
  at: string;
  /** The contract version, stamped on every frame rather than published only at
   * a discovery endpoint a consumer may never fetch. */
  v?: number;
  occupied?: boolean;
  /** On `room.occupancy_changed`: how sure Wavr is that the room is occupied.
   * Distinct from `precision`, which bounds granularity. */
  confidence?: number | null;
  count?: number | null;
  count_known?: boolean;
  previous?: unknown;
  precision?: Precision;
  how_to_improve?: string;
  /**
   * On `sensor.offline` / `sensor.online`: the sensor, described rather than
   * named. Derived exactly as `ContextSensor.label` is — the stream never
   * carries a `sensor_id`, which in this codebase is very often the name an
   * operator typed for their own camera.
   *
   * Stable for the life of the stream, so an `offline` and the `online` that
   * follows it name the same sensor identically. It is a display name, not a
   * join key: a context response numbers its own census, so "camera 1" in an
   * event and "camera 1" in a context are not promised to be the same camera.
   */
  label?: string;
  modality?: string;
  source?: SensorSource;
  simulated?: boolean;
  /** On `room.sensors_disagree` / `room.sensors_agree`: who says what, with the
   * same derived description as above. */
  sensors?: Array<{
    label: string;
    modality: string;
    source: SensorSource;
    simulated: boolean;
    says: "occupied" | "empty";
  }>;
}

export interface Subscription {
  /** `false` during a reconnect. Render from last known state rather than
   * blanking. */
  readonly connected: boolean;
  close(): void;
}

export interface ExperienceManifest {
  id?: string;
  experience_id?: string;
  name: string;
  version?: string;
  requires?: Capability[];
  optional?: Capability[];
  scopes?: string[];
  rooms?: string[];
  description?: string;
}

export interface Verdict {
  status: "FULLY_SUPPORTED" | "PARTIALLY_SUPPORTED" | "UNSUPPORTED";
  room: string;
  usable: boolean;
  satisfied: Capability[];
  missing_required: Capability[];
  missing_optional: Capability[];
  reasons: string[];
  fallbacks: string[];
}

export interface SessionTarget {
  device_id: string;
  /** Derived the same way, and for the same reason, as `ContextDevice.label`
   * — `experience_session` builds it and never accepts one from the caller. */
  label: string;
  /** What this device can be handed. Only capabilities it SAID it has. */
  can: Array<"display" | "audio">;
}

export interface ExperienceSession {
  session_id: string;
  experience_id: string;
  room: string;
  devices: string[];
  targets: SessionTarget[];
  /** Opaque to Wavr, bounded, and never interpreted. Your bookkeeping. */
  metadata: Record<string, string | number | boolean | null>;
}

export interface SessionEvent {
  event:
    | "experience.room_changed"
    | "experience.target_available"
    | "experience.target_lost";
  session_id: string;
  experience_id: string;
  room?: string;
  previous?: string;
  target?: SessionTarget;
}

export interface WavrClientOptions {
  baseUrl?: string;
  token?: string;
  fetch?: typeof fetch;
}

export class WavrClient {
  constructor(opts?: WavrClientOptions);
  baseUrl: string;
  token: string;
  space: { space_id: string; name: string } | null;
  protocolVersion: number | null;
  /** True when the Core speaks a newer contract than this SDK. Surfaced rather
   * than thrown: refusing would break every app the day a Core updates. */
  protocolAhead: boolean;

  connect(): Promise<this>;
  spaceContext(): Promise<Record<string, unknown>>;
  rooms(): Promise<string[]>;
  context(): Promise<RoomContext[]>;
  context(room: string): Promise<RoomContext>;
  anchors(room?: string): Promise<Anchor[]>;
  resolveAnchor(providerId: string, externalId: string): Promise<Anchor[]>;
  coverage(): Promise<Record<string, unknown>>;
  compatibility(
    manifest: ExperienceManifest,
    room?: string,
  ): Promise<Verdict & Record<string, unknown>>;
  scopes(): Promise<{ scopes: string[]; identity: string }>;
  openSession(
    experienceId: string,
    opts?: { room?: string; metadata?: Record<string, string | number | boolean> },
  ): Promise<ExperienceSession>;
  observeSession(
    sessionId: string,
    opts?: { room?: string },
  ): Promise<ExperienceSession & { events: SessionEvent[] }>;
  joinSession(sessionId: string, deviceId: string): Promise<ExperienceSession>;
  closeSession(sessionId: string): Promise<{ session_id: string; closed: boolean }>;
  subscribe(
    handler: (event: SpatialEvent) => void,
    opts?: { room?: string; since?: string; onError?: (e: WavrError) => void },
  ): Subscription;
}
