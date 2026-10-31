use std::{path::PathBuf, process::ExitCode};

fn usage() -> &'static str {
    "usage: observer-coc-helper --peer AA:BB:CC:DD:EE:FF --adapter hci0 --data-socket PATH --control-socket PATH | --test-mode --data-socket PATH --control-socket PATH"
}

fn arguments() -> Result<(bool, Option<String>, String, PathBuf, PathBuf), String> {
    let mut values = std::env::args().skip(1).collect::<Vec<_>>();
    let test_mode = values.first().is_some_and(|arg| arg == "--test-mode");
    if test_mode { values.remove(0); }
    let mut peer = None;
    let mut adapter = None;
    let mut data = None;
    let mut control = None;
    let mut args = values.into_iter();
    while let Some(flag) = args.next() {
        let value = args.next().ok_or_else(|| usage().to_owned())?;
        match flag.as_str() {
            "--peer" => peer = Some(value),
            "--adapter" => adapter = Some(value),
            "--data-socket" => data = Some(PathBuf::from(value)),
            "--control-socket" => control = Some(PathBuf::from(value)),
            _ => return Err(usage().to_owned()),
        }
    }
    let peer = if test_mode { if peer.is_some() { return Err(usage().into()); } None }
        else { Some(peer.ok_or_else(|| usage().to_owned())?) };
    Ok((test_mode, peer, adapter.unwrap_or_else(|| "hci0".into()),
        data.ok_or_else(|| usage().to_owned())?, control.ok_or_else(|| usage().to_owned())?))
}

async fn run_test_mode(data_path: &std::path::Path, control_path: &std::path::Path) -> std::io::Result<()> {
    use observer_coc_helper::{bind_local_socket, read_local_frame, write_local_frame};
    use tokio::io::AsyncReadExt;
    if data_path == control_path { return Err(std::io::Error::new(std::io::ErrorKind::InvalidInput, "IPC paths must differ")); }
    let data_listener = bind_local_socket(data_path)?;
    let control_listener = match bind_local_socket(control_path) {
        Ok(listener) => listener,
        Err(error) => { let _ = std::fs::remove_file(data_path); return Err(error); }
    };
    let result = async {
        let (mut data, _) = data_listener.accept().await?;
        let (mut control, _) = control_listener.accept().await?;
        let session = 0x102030405060708u64;
        let mut hello = vec![1, 1]; hello.extend_from_slice(&session.to_le_bytes()); hello.extend_from_slice(&0u32.to_le_bytes()); hello.extend_from_slice(&[0x20, 0x02]);
        write_local_frame(&mut data, &hello).await?;
        write_local_frame(&mut control, &hello).await?;
        for socket in [&mut data, &mut control] {
            let ready = read_local_frame(socket).await?;
            if ready.len() != 16 || ready[0] != 1 || ready[1] != 2 { return Err(std::io::Error::new(std::io::ErrorKind::InvalidData, "bad test handshake")); }
        }
        let mut record = vec![1, 3]; record.extend_from_slice(&session.to_le_bytes()); record.extend_from_slice(&1u32.to_le_bytes()); record.extend_from_slice(b"synthetic\r\n");
        write_local_frame(&mut data, &record).await?;
        let receipt = read_local_frame(&mut control).await?;
        if receipt.len() != 14 || receipt[1] != 4 || u32::from_le_bytes(receipt[10..14].try_into().unwrap()) != 1 {
            return Err(std::io::Error::new(std::io::ErrorKind::InvalidData, "bad test receipt"));
        }
        let command = read_local_frame(&mut control).await?;
        if command.len() < 15 || command[1] != 5 { return Err(std::io::Error::new(std::io::ErrorKind::InvalidData, "bad test command")); }
        let id = u32::from_le_bytes(command[10..14].try_into().unwrap());
        let mut reply = vec![1, 6]; reply.extend_from_slice(&session.to_le_bytes()); reply.extend_from_slice(&id.to_le_bytes()); reply.extend_from_slice(b"-> > synthetic-name\r\n");
        write_local_frame(&mut control, &reply).await?;
        let mut data_byte = [0; 1];
        let mut control_byte = [0; 1];
        tokio::select! { _ = data.read(&mut data_byte) => {}, _ = control.read(&mut control_byte) => {} }
        Ok::<(), std::io::Error>(())
    }.await;
    let _ = std::fs::remove_file(data_path);
    let _ = std::fs::remove_file(control_path);
    result
}

#[tokio::main]
async fn main() -> ExitCode {
    let (test_mode, peer, adapter, data, control) = match arguments() {
        Ok(values) => values,
        Err(error) => { eprintln!("{error}"); return ExitCode::from(2); }
    };
    let result = if test_mode {
        run_test_mode(&data, &control).await.map_err(|_| ())
    } else {
        observer_coc_helper::run(peer.as_deref().unwrap(), &adapter, &data, &control).await.map_err(|_| ())
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(_) => {
            // Do not print BlueZ/backend details, peer identifiers or payload-bearing errors.
            eprintln!("CoC helper stopped (security, discovery, channel, or IPC failure)");
            ExitCode::FAILURE
        }
    }
}
