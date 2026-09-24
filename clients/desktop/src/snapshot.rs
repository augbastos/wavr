use serde_json::Value;

#[derive(Debug, Default)]
pub struct Snapshot {
    pub schema: Option<u64>,
    pub reachable: Option<bool>,
    pub error: Option<String>,
    pub exit_code: Option<i64>,
    pub runtime: Option<Runtime>,
    pub attention: Option<Attention>,
    pub rooms: Option<Vec<Room>>,
    pub rooms_readable: Option<bool>,
    pub privacy: Option<Privacy>,
}

#[derive(Debug, Default)]
pub struct Runtime {
    pub state: Option<String>,
    pub headline: Option<String>,
    pub space: Option<String>,
    pub role: Option<String>,
    pub uptime_s: Option<f64>,
    pub last_state_age_s: Option<f64>,
    pub findings: Option<Vec<Finding>>,
}

#[derive(Debug, Default)]
pub struct Finding {
    pub key: Option<String>,
    pub state: Option<String>,
    pub text: Option<String>,
    pub detail: Option<String>,
}

#[derive(Debug, Default)]
pub struct Attention {
    pub total: Option<u64>,
    pub blocking: Option<u64>,
    pub degraded: Option<u64>,
    pub info: Option<u64>,
    pub headline: Option<String>,
    pub could_not_check: Option<Vec<String>>,
    pub items: Option<Vec<AttentionItem>>,
}

#[derive(Debug, Default)]
pub struct AttentionItem {
    pub key: Option<String>,
    pub band: Option<String>,
    pub title: Option<String>,
    pub detail: Option<String>,
    pub r#where: Option<String>,
    pub action: Option<String>,
    pub since: Option<String>,
    pub count: Option<u64>,
}

#[derive(Debug, Default)]
pub struct Room {
    pub room: Option<String>,
    pub occupied: Option<bool>,
    pub confidence: Option<f64>,
    pub person_count: Option<u64>,
    pub precision_level: Option<String>,
    pub explanation: Option<String>,
    pub ts: Option<String>,
    pub watch: Option<bool>,
    pub unrecognized: Option<bool>,
    pub sources: Option<Vec<Source>>,
}

#[derive(Debug, Default)]
pub struct Source {
    pub modality: Option<String>,
    pub sensor_id: Option<String>,
    pub presence: Option<bool>,
    pub confidence: Option<f64>,
    pub age_s: Option<f64>,
    pub health: Option<String>,
    pub count: Option<u64>,
}

#[derive(Debug, Default)]
pub struct Privacy {
    pub watch: Option<bool>,
}

fn field<'a>(value: &'a Value, name: &str) -> Option<&'a Value> {
    value.as_object()?.get(name)
}

fn string(value: &Value, name: &str) -> Option<String> {
    field(value, name)?.as_str().map(str::to_owned)
}

fn bool_field(value: &Value, name: &str) -> Option<bool> {
    field(value, name)?.as_bool()
}

fn uint(value: &Value, name: &str) -> Option<u64> {
    field(value, name)?.as_u64()
}

fn integer(value: &Value, name: &str) -> Option<i64> {
    field(value, name)?.as_i64()
}

fn number(value: &Value, name: &str) -> Option<f64> {
    field(value, name)?.as_f64().filter(|v| v.is_finite())
}

fn object<T>(value: &Value, name: &str, parse: impl FnOnce(&Value) -> T) -> Option<T> {
    let child = field(value, name)?;
    child.as_object()?;
    Some(parse(child))
}

fn array<T>(value: &Value, name: &str, parse: impl Fn(&Value) -> T) -> Option<Vec<T>> {
    Some(field(value, name)?.as_array()?.iter().filter(|v| v.is_object()).map(parse).collect())
}

impl Snapshot {
    pub fn parse(json: &str) -> Result<Self, serde_json::Error> {
        let value: Value = serde_json::from_str(json)?;
        Ok(Self {
            schema: uint(&value, "schema"),
            reachable: bool_field(&value, "reachable"),
            error: string(&value, "error"),
            exit_code: integer(&value, "exit_code"),
            runtime: object(&value, "runtime", Runtime::from_value),
            attention: object(&value, "attention", Attention::from_value),
            rooms: array(&value, "rooms", Room::from_value),
            rooms_readable: bool_field(&value, "rooms_readable"),
            privacy: object(&value, "privacy", Privacy::from_value),
        })
    }
}

impl Runtime {
    fn from_value(v: &Value) -> Self {
        Self { state: string(v, "state"), headline: string(v, "headline"), space: string(v, "space"), role: string(v, "role"), uptime_s: number(v, "uptime_s"), last_state_age_s: number(v, "last_state_age_s"), findings: array(v, "findings", Finding::from_value) }
    }
}

impl Finding {
    fn from_value(v: &Value) -> Self {
        Self { key: string(v, "key"), state: string(v, "state"), text: string(v, "text"), detail: string(v, "detail") }
    }
}

impl Attention {
    fn from_value(v: &Value) -> Self {
        Self { total: uint(v, "total"), blocking: uint(v, "blocking"), degraded: uint(v, "degraded"), info: uint(v, "info"), headline: string(v, "headline"), could_not_check: field(v, "could_not_check").and_then(Value::as_array).map(|a| a.iter().filter_map(Value::as_str).map(str::to_owned).collect()), items: array(v, "items", AttentionItem::from_value) }
    }
}

impl AttentionItem {
    fn from_value(v: &Value) -> Self {
        Self { key: string(v, "key"), band: string(v, "band"), title: string(v, "title"), detail: string(v, "detail"), r#where: string(v, "where"), action: string(v, "action"), since: string(v, "since"), count: uint(v, "count") }
    }
}

impl Room {
    fn from_value(v: &Value) -> Self {
        Self { room: string(v, "room"), occupied: bool_field(v, "occupied"), confidence: number(v, "confidence"), person_count: uint(v, "person_count"), precision_level: string(v, "precision_level"), explanation: string(v, "explanation"), ts: string(v, "ts"), watch: bool_field(v, "watch"), unrecognized: bool_field(v, "unrecognized"), sources: array(v, "sources", Source::from_value) }
    }
}

impl Source {
    fn from_value(v: &Value) -> Self {
        Self { modality: string(v, "modality"), sensor_id: string(v, "sensor_id"), presence: bool_field(v, "presence"), confidence: number(v, "confidence"), age_s: number(v, "age_s"), health: string(v, "health"), count: uint(v, "count") }
    }
}

impl Privacy {
    fn from_value(v: &Value) -> Self { Self { watch: bool_field(v, "watch") } }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn healthy_sample_preserves_nested_evidence() {
        let json = r#"{"schema":1,"reachable":true,"error":null,"exit_code":1,"runtime":{"state":"healthy","headline":"Wavr is running","space":"Home","role":"core","uptime_s":3600,"last_state_age_s":4.5,"findings":[{"key":"sensors","state":"healthy","text":"3 sensors","detail":null}]},"attention":{"total":1,"blocking":0,"degraded":1,"info":0,"headline":"1 thing needs your attention","could_not_check":[],"items":[{"key":"cam-url","band":"degraded","title":"Camera needs a URL","detail":"Kitchen camera","where":"kitchen","action":"open","since":"2026-09-24T10:00:00+00:00","count":1}]},"rooms":[{"room":"office","occupied":true,"confidence":0.87,"person_count":1,"precision_level":"position","explanation":"mmwave: presence","ts":"2026-09-24T10:00:00+00:00","watch":false,"unrecognized":false,"sources":[{"modality":"mmwave","sensor_id":"a","presence":true,"confidence":0.9,"age_s":3,"health":"fresh","count":1}]}],"rooms_readable":true,"privacy":{"watch":false}}"#;
        let s = Snapshot::parse(json).unwrap();
        assert_eq!(s.runtime.as_ref().unwrap().space.as_deref(), Some("Home"));
        assert_eq!(s.attention.as_ref().unwrap().items.as_ref().unwrap()[0].title.as_deref(), Some("Camera needs a URL"));
        assert_eq!(s.rooms.as_ref().unwrap()[0].sources.as_ref().unwrap()[0].modality.as_deref(), Some("mmwave"));
        assert_eq!(s.exit_code, Some(1));
    }

    #[test]
    fn unreachable_sample_keeps_unknown_runtime() {
        let s = Snapshot::parse(r#"{"schema":1,"reachable":false,"error":"cannot connect to 127.0.0.1:8000","exit_code":2,"runtime":null,"attention":null,"rooms":[],"rooms_readable":false,"privacy":{"watch":false}}"#).unwrap();
        assert_eq!(s.reachable, Some(false));
        assert!(s.runtime.is_none());
        assert!(s.attention.is_none());
        assert_eq!(s.rooms_readable, Some(false));
    }

    #[test]
    fn garbled_fields_do_not_discard_good_fields() {
        let s = Snapshot::parse(r#"{"reachable":"false","runtime":{"state":55,"space":"Home"},"attention":{"total":"0","items":[{"title":"Check","count":false}]},"rooms":[{"room":"office","occupied":0,"sources":[{"modality":"ble","age_s":"old"}]}],"privacy":{"watch":"false"}}"#).unwrap();
        assert_eq!(s.reachable, None);
        assert_eq!(s.runtime.as_ref().unwrap().state, None);
        assert_eq!(s.runtime.as_ref().unwrap().space.as_deref(), Some("Home"));
        assert_eq!(s.attention.as_ref().unwrap().total, None);
        assert_eq!(s.rooms.as_ref().unwrap()[0].occupied, None);
        assert_eq!(s.rooms.as_ref().unwrap()[0].sources.as_ref().unwrap()[0].modality.as_deref(), Some("ble"));
        assert_eq!(s.privacy.as_ref().unwrap().watch, None);
    }
}
