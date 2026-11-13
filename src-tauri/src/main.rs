#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    let args: Vec<_> = std::env::args_os().collect();
    if args.get(1).is_some_and(|arg| arg == "--apply-update") {
        if args.len() != 3 {
            std::process::exit(1);
        }
        std::process::exit(tts_lab_desktop::update_apply::run(std::path::Path::new(
            &args[2],
        )));
    }
    tts_lab_desktop::desktop::run();
}
