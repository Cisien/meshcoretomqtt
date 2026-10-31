use std::{io, path::Path};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};

pub const MAX_SDU: usize = 558;
const ATT_COMMAND_TIMEOUT: &str = "12s";
// Bluefruit's 128-bit UUID byte arrays are emitted in ATT little-endian order;
// BlueZ exposes the canonical string for those wire bytes (the reversed form).
pub const SERVICE_UUID: &str = "10c25d4a-3c61-50a3-5e48-566b026f5f1b";
pub const DISCOVERY_UUID: &str = "10c25d4a-3c61-50a3-5e48-566b076f5f1b";

pub fn parse_descriptor(bytes: &[u8]) -> io::Result<(u16, u16)> {
    if bytes.len() != 8 || bytes[0] != 1 || bytes[1] != 2 || u16::from_le_bytes([bytes[6], bytes[7]]) as usize != MAX_SDU {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "discovery descriptor"));
    }
    let data = u16::from_le_bytes([bytes[2], bytes[3]]);
    let control = u16::from_le_bytes([bytes[4], bytes[5]]);
    if data != 0x80 || control != 0x81 { return Err(io::Error::new(io::ErrorKind::InvalidData, "channel PSMs")); }
    Ok((data, control))
}

pub fn security_ok(sec: bluer::l2cap::Security) -> bool {
    sec.level == bluer::l2cap::SecurityLevel::High
}

fn bluez_error_category(kind: &bluer::ErrorKind) -> &'static str {
    use bluer::ErrorKind as E;
    match kind {
        E::AlreadyConnected => "already_connected",
        E::InProgress => "in_progress",
        E::ConnectionAttemptFailed => "connection_attempt_failed",
        E::AuthenticationCanceled | E::AuthenticationFailed | E::AuthenticationRejected | E::AuthenticationTimeout => "authentication",
        E::NotAuthorized | E::NotPermitted => "not_authorized",
        E::NotReady => "not_ready",
        E::NotFound | E::DoesNotExist => "not_found",
        E::Internal(bluer::InternalErrorKind::DBusConnectionLost) => "dbus_connection_lost",
        E::Internal(bluer::InternalErrorKind::Io(_)) => "io",
        E::Internal(_) => "internal",
        _ => "other",
    }
}

async fn wait_for_send_mtu<F>(mut read_mtu: F, timeout: std::time::Duration) -> io::Result<usize>
where
    F: FnMut() -> io::Result<usize>,
{
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        match read_mtu() {
            Ok(mtu) => return Ok(mtu),
            Err(error) if error.kind() == io::ErrorKind::NotConnected && tokio::time::Instant::now() < deadline => {
                tokio::time::sleep(std::time::Duration::from_millis(50)).await;
            }
            Err(error) => return Err(error),
        }
    }
}

fn mtus_support_advertised_sdu(send_mtu: usize, recv_mtu: usize) -> bool {
    send_mtu >= MAX_SDU && recv_mtu >= MAX_SDU
}

fn configure_coc_socket(socket: &bluer::l2cap::Socket<bluer::l2cap::SeqPacket>) -> io::Result<()> {
    // Match BlueR's SeqPacket::connect: bind an LE source before configuring
    // and connecting, so Linux selects the LE credit-based channel mode.
    socket.bind(bluer::l2cap::SocketAddr::any_le())?;
    socket.set_recv_mtu(MAX_SDU as u16)?;
    socket.set_security(bluer::l2cap::Security { level: bluer::l2cap::SecurityLevel::High, key_size: 0 })?;
    Ok(())
}

pub fn peer_properties_ok(paired: bool, trusted: bool, bonded: bool, blocked: bool) -> bool {
    paired && trusted && bonded && !blocked
}

pub fn discovery_security_ok(flags: bluer::gatt::CharacteristicFlags) -> bool {
    // Remote ATT declarations expose characteristic properties, not the
    // server's encrypted/MITM attribute permission. The later ReadValue request
    // is gated by the firmware ACL; local bond checks remain mandatory above.
    flags.read
}

fn att_hex_field(line: &str, marker: &str) -> Option<u16> {
    let value = line.split_once(marker)?.1.trim_start();
    let value = value.strip_prefix("0x").or_else(|| value.strip_prefix("0X"))?;
    let digits = value.chars().take_while(|c| c.is_ascii_hexdigit()).collect::<String>();
    if digits.is_empty() { return None; }
    u16::from_str_radix(&digits, 16).ok()
}

fn parse_att_service_range(output: &str) -> io::Result<(u16, u16)> {
    let mut found = None;
    for line in output.lines() {
        let Some((_, uuid)) = line.split_once("uuid:") else { continue; };
        if !uuid.trim().eq_ignore_ascii_case(SERVICE_UUID) { continue; }
        let start = att_hex_field(line, "attr handle =");
        let end = att_hex_field(line, "end grp handle =");
        let Some((start, end)) = start.zip(end).filter(|(start, end)| *start > 0 && start <= end) else {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "invalid CoC ATT service range"));
        };
        if found.replace((start, end)).is_some() {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "duplicate CoC ATT service"));
        }
    }
    found.ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "CoC service absent in fresh ATT discovery"))
}

fn parse_att_discovery_handle(output: &str, range: (u16, u16)) -> io::Result<u16> {
    let mut found = None;
    for line in output.lines() {
        let Some((_, uuid)) = line.split_once("uuid =") else { continue; };
        if !uuid.trim().eq_ignore_ascii_case(DISCOVERY_UUID) { continue; }
        let handle = att_hex_field(line, "char value handle =");
        let properties = att_hex_field(line, "char properties =");
        let Some(handle) = handle.filter(|h| range.0 <= *h && *h <= range.1) else {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "CoC ATT characteristic outside service range"));
        };
        if !properties.is_some_and(|bits| bits & 0x02 != 0) {
            return Err(io::Error::new(io::ErrorKind::PermissionDenied, "CoC ATT discovery characteristic is not readable"));
        }
        if found.replace(handle).is_some() {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "duplicate CoC ATT discovery characteristic"));
        }
    }
    found.ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "CoC discovery characteristic absent in fresh ATT discovery"))
}

fn att_characteristics_args(range: (u16, u16)) -> [String; 3] {
    ["--characteristics".to_owned(), format!("--start=0x{:04x}", range.0), format!("--end=0x{:04x}", range.1)]
}

fn parse_att_read_value(output: &str) -> io::Result<Vec<u8>> {
    let line = output.lines().find_map(|line| {
        line.split_once("Characteristic value/descriptor:").map(|(_, value)| value.trim())
            .or_else(|| line.split_once("Characteristic value:").map(|(_, value)| value.trim()))
            .or_else(|| line.split_once("Value:").map(|(_, value)| value.trim()))
    }).ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "guarded ATT read returned no value"))?;
    let bytes = line.split_whitespace().map(|token| {
        if token.len() != 2 { return Err(io::Error::new(io::ErrorKind::InvalidData, "invalid ATT value encoding")); }
        u8::from_str_radix(token, 16).map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "invalid ATT value encoding"))
    }).collect::<io::Result<Vec<_>>>()?;
    if bytes.len() != 8 { return Err(io::Error::new(io::ErrorKind::InvalidData, "invalid CoC descriptor length")); }
    Ok(bytes)
}

fn fresh_att_read_descriptor(peer: bluer::Address, adapter: &str, addr_type: bluer::AddressType) -> io::Result<Vec<u8>> {
    use std::process::Command;
    let addr_type = match addr_type {
        bluer::AddressType::LePublic => "public",
        bluer::AddressType::LeRandom => "random",
        _ => return Err(io::Error::new(io::ErrorKind::InvalidInput, "not a LE peer")),
    };
    let invoke = |label: &str, args: &[&str]| -> io::Result<String> {
        let output = Command::new("/usr/bin/timeout")
            .args(["--signal=TERM", ATT_COMMAND_TIMEOUT, "/usr/bin/gatttool", "-i", adapter, "-b", &peer.to_string(), "-t", addr_type, "-l", "high"])
            .args(args)
            .output()
            .map_err(|error| {
                eprintln!("CoC stage=att_{}_exec_failed kind={:?} errno={:?}", label, error.kind(), error.raw_os_error());
                error
            })?;
        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr).to_ascii_lowercase();
            let category = if output.status.code() == Some(124) { "timeout" }
                else if stderr.contains("connect error") || stderr.contains("failed to connect") { "connect" }
                else if stderr.contains("authentication") || stderr.contains("encryption") || stderr.contains("security") { "security" }
                else if stderr.contains("permission denied") || stderr.contains("operation not permitted") { "permission" }
                else { "other" };
            eprintln!("CoC stage=att_{}_failed exit={:?} category={}", label, output.status.code(), category);
            return Err(io::Error::new(io::ErrorKind::PermissionDenied, "bounded authenticated ATT operation failed"));
        }
        String::from_utf8(output.stdout).map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "invalid ATT discovery output"))
    };
    let primary = invoke("primary", &["--primary"])?;
    let range = parse_att_service_range(&primary).map_err(|error| {
        eprintln!("CoC stage=att_primary_parse_failed kind={:?}", error.kind()); error
    })?;
    eprintln!("CoC stage=att_primary_service_found");
    let args = att_characteristics_args(range);
    let characteristics = invoke("characteristics", &[args[0].as_str(), args[1].as_str(), args[2].as_str()])?;
    let handle = parse_att_discovery_handle(&characteristics, range).map_err(|error| {
        eprintln!("CoC stage=att_characteristics_parse_failed kind={:?}", error.kind()); error
    })?;
    eprintln!("CoC stage=att_discovery_characteristic_found");
    let handle = format!("0x{:04x}", handle);
    let value = invoke("guarded_read", &["--char-read", "--handle", &handle])?;
    parse_att_read_value(&value).map_err(|error| {
        eprintln!("CoC stage=att_guarded_read_parse_failed kind={:?}", error.kind()); error
    })
}

async fn wait_for_services_resolved<F, Fut>(
    mut check: F,
    timeout: std::time::Duration,
    interval: std::time::Duration,
) -> Result<(), Box<dyn std::error::Error>>
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = Result<bool, dbus::Error>>,
{
    let wait = async {
        loop {
            if check().await? { return Ok::<(), dbus::Error>(()); }
            tokio::time::sleep(interval).await;
        }
    };
    match tokio::time::timeout(timeout, wait).await {
        Ok(result) => result.map_err(Into::into),
        Err(_) => Err(io::Error::new(io::ErrorKind::TimedOut, "BlueZ service resolution timed out").into()),
    }
}

pub async fn read_local_frame<R: AsyncRead + Unpin>(reader: &mut R) -> io::Result<Vec<u8>> {
    let len = reader.read_u16_le().await? as usize;
    if len == 0 || len > MAX_SDU { return Err(io::Error::new(io::ErrorKind::InvalidData, "local SDU length")); }
    let mut frame = vec![0; len];
    reader.read_exact(&mut frame).await?;
    Ok(frame)
}

pub async fn write_local_frame<W: AsyncWrite + Unpin>(writer: &mut W, frame: &[u8]) -> io::Result<()> {
    if frame.is_empty() || frame.len() > MAX_SDU { return Err(io::Error::new(io::ErrorKind::InvalidData, "local SDU length")); }
    writer.write_u16_le(frame.len() as u16).await?;
    writer.write_all(frame).await?;
    writer.flush().await
}

pub async fn read_descriptor(session: &bluer::Session, peer: &str, adapter_name: &str) -> Result<(bluer::Device, bluer::Address, bluer::AddressType, u16, u16), Box<dyn std::error::Error>> {
    use dbus::blocking::stdintf::org_freedesktop_dbus::Properties;
    eprintln!("CoC stage=bluez_session");
    let address: bluer::Address = peer.parse()?;
    let adapter = session.adapter(adapter_name)?;
    let device = adapter.device(address)?;

    // Read only existing properties. Reject before Connect; never call Pair or scan.
    eprintln!("CoC stage=bond_properties");
    let (paired, trusted, blocked) = (device.is_paired().await?, device.is_trusted().await?, device.is_blocked().await?);
    if !paired || !trusted || blocked { return Err("peer must already be Paired and Trusted and not Blocked".into()); }
    if device.remote_address().await? != address { return Err("resolved peer identity mismatch".into()); }
    let object_path = format!("/org/bluez/{adapter_name}/dev_{}", address.to_string().replace(':', "_").to_uppercase());
    let dbus = dbus::blocking::Connection::new_system()?;
    let proxy = dbus.with_proxy("org.bluez", object_path, std::time::Duration::from_secs(3));
    let bonded: bool = proxy.get("org.bluez.Device1", "Bonded")?;
    if !peer_properties_ok(paired, trusted, bonded, blocked) { return Err("peer must already be Bonded".into()); }
    let addr_type = device.address_type().await?;
    if !matches!(addr_type, bluer::AddressType::LePublic | bluer::AddressType::LeRandom) {
        return Err("resolved peer is not a LE address".into());
    }
    eprintln!("CoC stage=gatt_connect");
    if let Err(error) = device.connect().await {
        eprintln!("CoC stage=gatt_connect_failed category={}", bluez_error_category(&error.kind));
        return Err(error.into());
    }
    let device_path = format!("/org/bluez/{adapter_name}/dev_{}", address.to_string().replace(':', "_").to_uppercase());
    let bluez = dbus.with_proxy("org.bluez", device_path, std::time::Duration::from_secs(3));
    eprintln!("CoC stage=services_resolution_wait");
    if let Err(error) = wait_for_services_resolved(
        || async { bluez.get("org.bluez.Device1", "ServicesResolved") },
        std::time::Duration::from_secs(6),
        std::time::Duration::from_millis(100),
    ).await {
        let _ = device.disconnect().await;
        return Err(error);
    }
    eprintln!("CoC stage=services_resolution resolved=true");
    let discovery = async {
        eprintln!("CoC stage=gatt_service_discovery");
        let mut descriptor = None;
        let services = device.services().await?;
        eprintln!("CoC stage=gatt_services_received count={}", services.len());
        let mut service_found = false;
        let mut characteristic_found = false;
        for service in services {
            if service.uuid().await?.to_string() != SERVICE_UUID { continue; }
            service_found = true;
            eprintln!("CoC stage=coc_service_found");
            let characteristics = service.characteristics().await?;
            eprintln!("CoC stage=gatt_characteristics_received count={}", characteristics.len());
            for characteristic in characteristics {
                if characteristic.uuid().await?.to_string() != DISCOVERY_UUID { continue; }
                characteristic_found = true;
                eprintln!("CoC stage=discovery_characteristic_found");
                let flags = characteristic.flags().await?;
                // ATT does not transmit remote attribute permission metadata. This
                // BlueZ flag is not evidence of the server's authenticated-read ACL.
                eprintln!("CoC stage=remote_characteristic_flags read={} authenticated_read={}", flags.read, flags.encrypt_authenticated_read);
                if !discovery_security_ok(flags) {
                    return Err::<_, Box<dyn std::error::Error>>("discovery characteristic lacks authenticated encrypted read".into());
                }
                eprintln!("CoC stage=descriptor_read");
                descriptor = Some(characteristic.read().await?);
            }
            if !characteristic_found { eprintln!("CoC stage=discovery_characteristic_missing"); }
        }
        if !service_found { eprintln!("CoC stage=coc_service_missing"); }
        if descriptor.is_none() && (!service_found || !characteristic_found) {
            // BlueZ may keep an old bonded GATT object graph after a database
            // shape change. Do not invalidate its cache: leave this connection,
            // perform bounded direct ATT discovery on the pinned peer with High
            // security, resolve the characteristic by UUID within the freshly
            // discovered service range, then read only that discovered handle.
            eprintln!("CoC stage=fresh_att_fallback");
            device.disconnect().await?;
            descriptor = Some(fresh_att_read_descriptor(address, adapter_name, addr_type)?);
            eprintln!("CoC stage=guarded_att_read_succeeded");
        }
        let bytes = descriptor.ok_or("secure CoC discovery characteristic missing")?;
        let (data_psm, control_psm) = parse_descriptor(&bytes)?;
        Ok::<_, Box<dyn std::error::Error>>((address, addr_type, data_psm, control_psm))
    }.await;
    match discovery {
        Ok((resolved, addr_type, data_psm, control_psm)) => Ok((device, resolved, addr_type, data_psm, control_psm)),
        Err(error) => { let _ = device.disconnect().await; Err(error) }
    }
}

pub async fn open_channel(peer: bluer::Address, peer_type: bluer::AddressType, psm: u16) -> io::Result<bluer::l2cap::SeqPacket> {
    use bluer::l2cap::{SeqPacket, Socket, SocketAddr};
    eprintln!("CoC stage=coc_socket_setup");
    let socket = Socket::<SeqPacket>::new_seq_packet()?;
    configure_coc_socket(&socket)?;
    eprintln!("CoC stage=coc_socket_configured");
    eprintln!("CoC stage=coc_connect");
    let channel = match socket.connect(SocketAddr::new(peer, peer_type, psm)).await {
        Ok(channel) => channel,
        Err(error) => {
            eprintln!("CoC stage=coc_connect_failed kind={:?} errno={:?}", error.kind(), error.raw_os_error());
            return Err(error);
        }
    };
    eprintln!("CoC stage=coc_connected");
    let remote = match channel.peer_addr() {
        Ok(remote) => remote,
        Err(error) => { eprintln!("CoC stage=peer_address_check_failed kind={:?} errno={:?}", error.kind(), error.raw_os_error()); return Err(error); }
    };
    eprintln!("CoC stage=peer_address_observed");
    let link_security = match channel.as_ref().security() {
        Ok(security) => security,
        Err(error) => { eprintln!("CoC stage=security_check_failed kind={:?} errno={:?}", error.kind(), error.raw_os_error()); return Err(error); }
    };
    let security_matches = security_ok(link_security);
    if link_security.key_size == 0 {
        eprintln!("CoC stage=security_verified level={:?} key_size=UNKNOWN match={}",
            link_security.level, security_matches);
    } else {
        eprintln!("CoC stage=security_verified level={:?} key_size={} match={}",
            link_security.level, link_security.key_size, security_matches);
    }
    if !security_matches {
        return Err(io::Error::new(io::ErrorKind::PermissionDenied, "CoC link did not meet High authenticated-encryption policy"));
    }
    let send_mtu = match wait_for_send_mtu(|| channel.send_mtu().map_err(io::Error::from), std::time::Duration::from_secs(3)).await {
        Ok(mtu) => mtu,
        Err(error) => { eprintln!("CoC stage=send_mtu_check_failed kind={:?} errno={:?}", error.kind(), error.raw_os_error()); return Err(error); }
    };
    eprintln!("CoC stage=send_mtu_observed value={}", send_mtu);
    let recv_mtu = match channel.recv_mtu() {
        Ok(mtu) => mtu,
        Err(error) => { eprintln!("CoC stage=recv_mtu_check_failed kind={:?} errno={:?}", error.kind(), error.raw_os_error()); return Err(error); }
    };
    eprintln!("CoC stage=recv_mtu_observed value={}", recv_mtu);
    let peer_ok = remote.addr == peer && remote.addr_type == peer_type && remote.psm == psm;
    let mtu_matches = mtus_support_advertised_sdu(send_mtu, recv_mtu);
    if !peer_ok || !security_matches || !mtu_matches {
        eprintln!("CoC stage=coc_link_rejected peer_match={} security_match={} send_mtu_match={} recv_mtu_match={}",
            peer_ok, security_matches, send_mtu >= MAX_SDU, recv_mtu >= MAX_SDU);
        return Err(io::Error::new(io::ErrorKind::PermissionDenied, "CoC peer/security/MTU verification failed"));
    }
    Ok(channel)
}

pub async fn bridge_channel(local: tokio::net::UnixStream, channel: bluer::l2cap::SeqPacket) -> io::Result<()> {
    let channel = std::sync::Arc::new(channel);
    let (mut local_read, mut local_write) = local.into_split();
    let to_radio = async {
        loop {
            let frame = read_local_frame(&mut local_read).await?;
            if channel.send(&frame).await? != frame.len() { return Err(io::Error::new(io::ErrorKind::WriteZero, "short CoC send")); }
        }
        #[allow(unreachable_code)] Ok::<(), io::Error>(())
    };
    let from_radio = async {
        let mut frame = vec![0; MAX_SDU];
        loop {
            let len = channel.recv(&mut frame).await?;
            if len == 0 || len > MAX_SDU { return Err(io::Error::new(io::ErrorKind::InvalidData, "invalid CoC SDU")); }
            write_local_frame(&mut local_write, &frame[..len]).await?;
        }
        #[allow(unreachable_code)] Ok::<(), io::Error>(())
    };
    tokio::select! {
        result = to_radio => result,
        result = from_radio => result,
    }
}

pub fn bind_local_socket(path: &Path) -> io::Result<tokio::net::UnixListener> {
    use std::os::unix::fs::PermissionsExt;
    if path.exists() { return Err(io::Error::new(io::ErrorKind::AlreadyExists, "refusing existing IPC path")); }
    let listener = std::os::unix::net::UnixListener::bind(path)?;
    listener.set_nonblocking(true)?;
    if let Err(error) = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600)) {
        drop(listener);
        let _ = std::fs::remove_file(path);
        return Err(error);
    }
    tokio::net::UnixListener::from_std(listener)
}

pub async fn accept_and_bridge(listener: tokio::net::UnixListener, channel: bluer::l2cap::SeqPacket) -> io::Result<()> {
    let (stream, _) = listener.accept().await?;
    bridge_channel(stream, channel).await
}

pub async fn run(peer: &str, adapter: &str, data_path: &Path, control_path: &Path) -> Result<(), Box<dyn std::error::Error>> {
    if data_path == control_path { return Err("data and control IPC paths must be distinct".into()); }
    eprintln!("CoC stage=session_create");
    let session = bluer::Session::new().await?;
    let (device, resolved, addr_type, data_psm, control_psm) = read_descriptor(&session, peer, adapter).await?;
    let result: Result<(), Box<dyn std::error::Error>> = async {
        eprintln!("CoC stage=data_channel");
        let data = open_channel(resolved, addr_type, data_psm).await?;
        eprintln!("CoC stage=control_channel");
        let control = open_channel(resolved, addr_type, control_psm).await?;
        eprintln!("CoC stage=ipc_bind");
        let data_listener = bind_local_socket(data_path)?;
        let control_listener = match bind_local_socket(control_path) {
            Ok(listener) => listener,
            Err(error) => { let _ = std::fs::remove_file(data_path); return Err(error.into()); }
        };
        eprintln!("CoC helper ready (2 channels; payload logging disabled)");
        let result = tokio::try_join!(
            accept_and_bridge(data_listener, data),
            accept_and_bridge(control_listener, control),
        );
        let _ = std::fs::remove_file(data_path);
        let _ = std::fs::remove_file(control_path);
        result?;
        Ok::<(), Box<dyn std::error::Error>>(())
    }.await;
    let _ = device.disconnect().await;
    result?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::AsyncWriteExt;

    #[test]
    fn bluefruit_uuid_bytes_match_bluez_canonical_uuid_strings() {
        assert_eq!(SERVICE_UUID, "10c25d4a-3c61-50a3-5e48-566b026f5f1b");
        assert_eq!(DISCOVERY_UUID, "10c25d4a-3c61-50a3-5e48-566b076f5f1b");
    }

    #[test]
    fn direct_att_parser_selects_discovery_characteristic_inside_exact_service_range() {
        let primary = "attr handle = 0x0001, end grp handle = 0x0005, uuid: 00001800-0000-1000-8000-00805f9b34fb\nattr handle = 0x0010, end grp handle = 0x0018, uuid: 10c25d4a-3c61-50a3-5e48-566b026f5f1b\n";
        let chars = "char value handle = 0x0017, char properties = 0x02, char ext properties = 0x0000, uuid = 10c25d4a-3c61-50a3-5e48-566b076f5f1b\n";
        let range = parse_att_service_range(primary).unwrap();
        assert_eq!(range, (0x10, 0x18));
        assert_eq!(parse_att_discovery_handle(chars, range).unwrap(), 0x17);
    }

    #[test]
    fn direct_att_parser_rejects_unreadable_duplicate_or_out_of_range_handles() {
        let range = (0x10, 0x18);
        assert!(parse_att_discovery_handle(
            "char value handle = 0x0017, char properties = 0x00, uuid = 10c25d4a-3c61-50a3-5e48-566b076f5f1b\n", range).is_err());
        assert!(parse_att_discovery_handle(
            "char value handle = 0x0019, char properties = 0x02, uuid = 10c25d4a-3c61-50a3-5e48-566b076f5f1b\n", range).is_err());
        assert!(parse_att_discovery_handle(
            "char value handle = 0x0017, char properties = 0x02, uuid = 10c25d4a-3c61-50a3-5e48-566b076f5f1b\nchar value handle = 0x0016, char properties = 0x02, uuid = 10c25d4a-3c61-50a3-5e48-566b076f5f1b\n", range).is_err());
    }

    #[test]
    fn direct_att_read_parser_decodes_only_an_exact_eight_byte_value() {
        assert_eq!(parse_att_read_value("Characteristic value/descriptor: 01 02 80 00 81 00 2e 02\n").unwrap(),
                   [1, 2, 0x80, 0, 0x81, 0, 0x2e, 2]);
        assert!(parse_att_read_value("Characteristic value/descriptor: 01 02 80 00\n").is_err());
    }

    #[test]
    fn gatttool_range_options_are_single_equals_form_arguments() {
        assert_eq!(att_characteristics_args((0x10, 0x18)),
                   ["--characteristics".to_owned(), "--start=0x0010".to_owned(), "--end=0x0018".to_owned()]);
        assert_eq!(ATT_COMMAND_TIMEOUT, "12s");
    }

    #[tokio::test]
    async fn service_resolution_waits_for_true_and_times_out_bounded() {
        use std::sync::{Arc, atomic::{AtomicUsize, Ordering}};
        let checks = Arc::new(AtomicUsize::new(0));
        let seen = checks.clone();
        wait_for_services_resolved(
            move || { let n = seen.fetch_add(1, Ordering::SeqCst); async move { Ok::<bool, dbus::Error>(n >= 2) } },
            std::time::Duration::from_millis(100), std::time::Duration::from_millis(1),
        ).await.unwrap();
        assert!(checks.load(Ordering::SeqCst) >= 3);

        let error = wait_for_services_resolved(
            || async { Ok::<bool, dbus::Error>(false) },
            std::time::Duration::from_millis(5), std::time::Duration::from_millis(1),
        ).await.unwrap_err();
        assert_eq!(error.downcast_ref::<io::Error>().unwrap().kind(), io::ErrorKind::TimedOut);
    }

    #[test]
    fn descriptor_requires_version_two_channels_psms_and_sdu() {
        assert_eq!(parse_descriptor(&[1, 2, 0x80, 0, 0x81, 0, 0x2e, 2]).unwrap(), (0x80, 0x81));
        for bad in [vec![0; 8], vec![1, 1, 0x80, 0, 0, 0, 0x2e, 2], vec![1, 2, 0, 0, 0x81, 0, 0x2e, 2], vec![1, 2, 0x80, 0, 0x80, 0, 0x2e, 2], vec![1, 2, 0x80, 0, 0x81, 0, 0, 0]] {
            assert!(parse_descriptor(&bad).is_err());
        }
    }

    #[test]
    fn approved_link_security_policy_requires_high_not_key_size() {
        assert!(security_ok(bluer::l2cap::Security { level: bluer::l2cap::SecurityLevel::High, key_size: 16 }));
        assert!(security_ok(bluer::l2cap::Security { level: bluer::l2cap::SecurityLevel::High, key_size: 7 }));
        assert!(security_ok(bluer::l2cap::Security { level: bluer::l2cap::SecurityLevel::High, key_size: 0 }));
        assert!(!security_ok(bluer::l2cap::Security { level: bluer::l2cap::SecurityLevel::Medium, key_size: 16 }));
        assert!(!security_ok(bluer::l2cap::Security { level: bluer::l2cap::SecurityLevel::Low, key_size: 16 }));
        assert!(!security_ok(bluer::l2cap::Security { level: bluer::l2cap::SecurityLevel::Sdp, key_size: 16 }));
    }

    #[test]
    fn bluez_failure_diagnostic_is_a_fixed_category() {
        assert_eq!(bluez_error_category(&bluer::ErrorKind::AlreadyConnected), "already_connected");
        assert_eq!(bluez_error_category(&bluer::ErrorKind::InProgress), "in_progress");
        assert_eq!(bluez_error_category(&bluer::ErrorKind::Internal(bluer::InternalErrorKind::DBus("private detail".into()))), "internal");
    }

    #[tokio::test]
    async fn send_mtu_retries_only_transient_not_connected_state() {
        let mut values = std::collections::VecDeque::from([
            Err(io::Error::from(io::ErrorKind::NotConnected)),
            Ok(558),
        ]);
        assert_eq!(wait_for_send_mtu(|| values.pop_front().unwrap(), std::time::Duration::from_millis(200)).await.unwrap(), 558);

        let error = wait_for_send_mtu(|| Err(io::Error::from(io::ErrorKind::PermissionDenied)), std::time::Duration::from_secs(1)).await.unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::PermissionDenied);
    }

    #[test]
    fn negotiated_mtu_must_cover_advertised_sdu_without_exact_equality() {
        assert!(mtus_support_advertised_sdu(558, 558));
        assert!(mtus_support_advertised_sdu(600, 700));
        assert!(!mtus_support_advertised_sdu(557, 558));
        assert!(!mtus_support_advertised_sdu(558, 557));
    }

    #[tokio::test]
    async fn le_coc_socket_setup_uses_supported_mtu_and_security_options() {
        let socket = bluer::l2cap::Socket::<bluer::l2cap::SeqPacket>::new_seq_packet().unwrap();
        configure_coc_socket(&socket).unwrap();
        assert!(socket.security().unwrap().level >= bluer::l2cap::SecurityLevel::High);
        assert_eq!(socket.recv_mtu().unwrap(), MAX_SDU as u16);
    }

    #[test]
    fn peer_requires_all_existing_security_properties() {
        assert!(peer_properties_ok(true, true, true, false));
        assert!(!peer_properties_ok(false, true, true, false));
        assert!(!peer_properties_ok(true, false, true, false));
        assert!(!peer_properties_ok(true, true, false, false));
        assert!(!peer_properties_ok(true, true, true, true));
    }

    #[test]
    fn remote_gatt_flags_require_read_but_do_not_claim_server_permissions() {
        assert!(discovery_security_ok(bluer::gatt::CharacteristicFlags { read: true, encrypt_authenticated_read: true, ..Default::default() }));
        assert!(discovery_security_ok(bluer::gatt::CharacteristicFlags { read: true, ..Default::default() }));
        assert!(!discovery_security_ok(bluer::gatt::CharacteristicFlags { encrypt_authenticated_read: true, ..Default::default() }));
    }

    #[tokio::test]
    async fn local_frame_is_bounded_and_exact_over_unix_pair() {
        let (mut a, mut b) = tokio::net::UnixStream::pair().unwrap();
        let task = tokio::spawn(async move { write_local_frame(&mut a, &[4, 5, 6]).await.unwrap() });
        assert_eq!(read_local_frame(&mut b).await.unwrap(), vec![4, 5, 6]);
        task.await.unwrap();
        drop(b);
        let (mut invalid_writer, mut invalid_reader) = tokio::net::UnixStream::pair().unwrap();
        invalid_writer.write_all(&559u16.to_le_bytes()).await.unwrap();
        assert!(read_local_frame(&mut invalid_reader).await.is_err());
    }

    #[tokio::test]
    async fn independent_local_channels_keep_control_live_when_data_stalls() {
        let (mut data_agent, data_peer) = tokio::net::UnixStream::pair().unwrap();
        let (mut ctl_agent, mut ctl_peer) = tokio::net::UnixStream::pair().unwrap();
        // A blocked data frame writer cannot own a mutex/global queue used by control.
        let stalled = tokio::spawn(async move {
            let frame = vec![7; MAX_SDU];
            loop { if write_local_frame(&mut data_agent, &frame).await.is_err() { break; } }
        });
        tokio::time::sleep(std::time::Duration::from_millis(30)).await;
        write_local_frame(&mut ctl_agent, &[1, 2, 3]).await.unwrap();
        assert_eq!(read_local_frame(&mut ctl_peer).await.unwrap(), vec![1, 2, 3]);
        stalled.abort();
        drop(data_peer);
    }

    #[tokio::test]
    async fn ipc_paths_are_private_and_existing_paths_are_never_removed_or_reused() {
        use std::{os::unix::fs::PermissionsExt, time::{SystemTime, UNIX_EPOCH}};
        let suffix = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let path = std::env::temp_dir().join(format!("observer-coc-{}-{suffix}.sock", std::process::id()));
        let listener = bind_local_socket(&path).unwrap();
        assert_eq!(std::fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o600);
        assert!(bind_local_socket(&path).is_err());
        drop(listener);
        std::fs::remove_file(path).unwrap();
    }
}
