// 까치녹음기.app 실행기 (작은 Cocoa 앱).
//
// - 켜지면 서버(.venv 의 uvicorn)를 자식 프로세스로 띄우고 브라우저로 화면을 연다.
// - Dock 아이콘을 다시 누르면 화면을 다시 연다.
// - ⌘Q / Dock 에서 종료하면 서버를 정상 종료시킨다 (받아쓰는 중이면 먼저 물어봄).
// - 화면의 '끄기'로 서버가 꺼지면 이 앱도 따라서 끝난다.
//
// 셸 스크립트로 서버를 띄우면 스크립트가 끝날 때 macOS 가 앱에 딸린 프로세스를 정리하면서
// 서버까지 꺼버린다 (새 세션으로 띄워도 마찬가지였음). 그래서 앱이 계속 살아 있는 구조로 만들었다.
//
// 빌드: scripts/build_launcher.sh  →  assets/kkachi-launcher

import AppKit

final class AppDelegate: NSObject, NSApplicationDelegate {
    let port = ProcessInfo.processInfo.environment["KKACHI_PORT"] ?? "8765"
    lazy var base = URL(string: "http://127.0.0.1:\(port)")!
    var server: Process?
    var watch: Timer?
    var misses = 0

    lazy var repo: String = {
        let file = Bundle.main.url(forResource: "repo_path", withExtension: nil)
        return (file.flatMap { try? String(contentsOf: $0, encoding: .utf8) } ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }()

    lazy var logURL: URL = {
        let dir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/KkachiNogeumgi/logs")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("server.log")
    }()

    func applicationDidFinishLaunching(_ note: Notification) {
        setupMenu()
        if isOurs() {               // 이미 켜져 있음 (예전 실행이 남아 있던 경우)
            openBrowser()
            startWatching()
            return
        }
        if portBusy() {
            fatal("까치녹음기를 켤 수 없어요", "다른 프로그램이 \(port)번 통로를 쓰고 있어요. 맥을 재시동한 뒤 다시 눌러주세요.")
            return
        }
        let python = repo + "/.venv/bin/python"
        guard FileManager.default.isExecutableFile(atPath: python) else {
            fatal("설치를 찾을 수 없어요", "설치 폴더가 옮겨졌거나 지워졌어요. 설치를 다시 해주세요.\n(\(repo))")
            return
        }
        startServer(python: python)
        DispatchQueue.global().async {
            for _ in 0..<120 {   // 최대 60초
                if self.isOurs() {
                    DispatchQueue.main.async { self.openBrowser(); self.startWatching() }
                    return
                }
                if self.server?.isRunning == false { break }
                Thread.sleep(forTimeInterval: 0.5)
            }
            DispatchQueue.main.async {
                self.fatal("까치녹음기를 켜지 못했어요", "잠시 후 다시 눌러보고, 계속되면 로그를 보내주세요.")
            }
        }
    }

    // Dock 아이콘을 다시 눌렀을 때
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openBrowser()
        return false
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if let busy = busyReason() {
            NSApp.activate(ignoringOtherApps: true)
            let a = NSAlert()
            a.messageText = "까치녹음기를 끌까요?"
            a.informativeText = "\(busy). 지금 끄면 그 녹음은 나중에 [다시 시도]를 눌러야 해요."
            a.addButton(withTitle: "끄기")
            a.addButton(withTitle: "취소")
            if a.runModal() != .alertFirstButtonReturn { return .terminateCancel }
        }
        stopServer()
        return .terminateNow
    }

    // MARK: 서버

    func startServer(python: String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", port]
        p.currentDirectoryURL = URL(fileURLWithPath: repo)
        var env = ProcessInfo.processInfo.environment
        env["HF_HUB_OFFLINE"] = "1"            // 실행 중에는 인터넷에 접속하지 않는다
        env["HF_HUB_DISABLE_TELEMETRY"] = "1"
        env["PYTHONWARNINGS"] = "ignore"
        p.environment = env
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        if let log = try? FileHandle(forWritingTo: logURL) {
            log.seekToEndOfFile()
            let stamp = DateFormatter.localizedString(from: Date(), dateStyle: .short, timeStyle: .medium)
            log.write("---- \(stamp) 앱 시작 ----\n".data(using: .utf8)!)
            p.standardOutput = log
            p.standardError = log
        }
        do { try p.run(); server = p } catch {
            fatal("까치녹음기를 켜지 못했어요", error.localizedDescription)
        }
    }

    func stopServer() {
        watch?.invalidate()
        _ = request("POST", "/api/app/quit", timeout: 2)
        if let p = server, p.isRunning {
            let deadline = Date().addingTimeInterval(8)
            while p.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.1) }
            if p.isRunning { p.terminate() }
        }
    }

    /// 서버가 화면의 '끄기'로 꺼지면 앱도 끝낸다. 업데이트 후 재시작(같은 프로세스 execv)은 기다린다.
    func startWatching() {
        watch = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            guard let self else { return }
            if let p = self.server {
                if !p.isRunning { NSApp.terminate(nil) }
                return
            }
            self.misses = self.isOurs() ? 0 : self.misses + 1
            if self.misses >= 10 { NSApp.terminate(nil) }  // 우리가 띄운 게 아니면 30초 동안 응답 없을 때
        }
    }

    // MARK: HTTP

    func request(_ method: String, _ path: String, timeout: TimeInterval = 2) -> (Int, Data)? {
        var req = URLRequest(url: base.appendingPathComponent(path))
        req.httpMethod = method
        req.timeoutInterval = timeout
        let sem = DispatchSemaphore(value: 0)
        var result: (Int, Data)?
        URLSession.shared.dataTask(with: req) { data, resp, _ in
            if let r = resp as? HTTPURLResponse { result = (r.statusCode, data ?? Data()) }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + timeout + 0.5)
        return result
    }

    func isOurs() -> Bool {
        guard let (code, data) = request("GET", "/api/health"), code == 200 else { return false }
        return String(data: data, encoding: .utf8)?.contains("\"app\":\"kkachi\"") ?? false
    }

    func portBusy() -> Bool { request("GET", "/") != nil }

    func busyReason() -> String? {
        guard let (code, data) = request("GET", "/api/app"), code == 200,
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let busy = obj["busy"] as? [String], let first = busy.first else { return nil }
        return first
    }

    // MARK: 화면

    func openBrowser() { NSWorkspace.shared.open(base) }

    @objc func openFromMenu() { openBrowser() }

    func fatal(_ title: String, _ message: String) {
        NSApp.activate(ignoringOtherApps: true)
        let a = NSAlert()
        a.alertStyle = .critical
        a.messageText = title
        a.informativeText = message
        a.runModal()
        server?.terminate()
        exit(1)
    }

    func setupMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let menu = NSMenu()
        menu.addItem(withTitle: "까치녹음기 열기", action: #selector(openFromMenu), keyEquivalent: "o")
        menu.addItem(.separator())
        menu.addItem(withTitle: "까치녹음기 끄기", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = menu
        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
