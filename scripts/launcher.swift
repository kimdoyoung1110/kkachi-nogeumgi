// 까치녹음기.app 실행기 (작은 Cocoa 앱).
//
// - 켜지면 서버(.venv 의 uvicorn)를 자식 프로세스로 띄우고, 화면을 '자체 창'(WKWebView)에 띄운다.
//   (브라우저 탭으로 열면 Dock 을 누를 때마다 탭이 새로 생겨서 바꿨다)
// - Dock 아이콘을 누르면 창을 앞으로. 창을 닫아도 앱은 켜져 있다 (녹음도 계속됨). ⌘Q 로 끈다.
// - ⌘Q / Dock 에서 종료하면 서버를 정상 종료시킨다 (받아쓰는 중이면 먼저 물어봄).
// - 화면의 '끄기'로 서버가 꺼지면 이 앱도 따라서 끝난다.
//
// 셸 스크립트로 서버를 띄우면 스크립트가 끝날 때 macOS 가 앱에 딸린 프로세스를 정리하면서
// 서버까지 꺼버린다 (새 세션으로 띄워도 마찬가지였음). 그래서 앱이 계속 살아 있는 구조로 만들었다.
//
// 빌드: scripts/build_launcher.sh  →  assets/kkachi-launcher

import AppKit
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate,
    WKUIDelegate, WKNavigationDelegate, WKDownloadDelegate {
    let port = ProcessInfo.processInfo.environment["KKACHI_PORT"] ?? "8765"
    lazy var base = URL(string: "http://127.0.0.1:\(port)/")!
    var server: Process?
    var watch: Timer?
    var misses = 0
    var window: NSWindow!
    var web: WKWebView!
    var loading: NSTextField!
    var downloads: [WKDownload: URL] = [:]

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
        makeWindow()
        if isOurs() {               // 이미 켜져 있음 (예전 실행이 남아 있던 경우)
            showPage()
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
                    DispatchQueue.main.async { self.showPage(); self.startWatching() }
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

    // Dock 아이콘을 다시 눌렀을 때: 창을 앞으로 (새 창·탭을 만들지 않음)
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showWindow()
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

    // MARK: 창

    func makeWindow() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()          // 첫 실행 안내·고른 분류 같은 설정 기억
        config.mediaTypesRequiringUserActionForPlayback = []
        config.applicationNameForUserAgent = "KkachiApp/1.0"   // 화면이 앱 창 안인지 알 수 있게
        web = WKWebView(frame: .zero, configuration: config)
        web.uiDelegate = self
        web.navigationDelegate = self
        web.allowsBackForwardNavigationGestures = true
        web.isHidden = true

        loading = NSTextField(labelWithString: "까치녹음기를 켜는 중…")
        loading.font = .systemFont(ofSize: 15)
        loading.textColor = .secondaryLabelColor
        loading.alignment = .center

        let content = NSView()
        for v in [web!, loading!] as [NSView] {
            v.translatesAutoresizingMaskIntoConstraints = false
            content.addSubview(v)
        }
        NSLayoutConstraint.activate([
            web.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            web.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            web.topAnchor.constraint(equalTo: content.topAnchor),
            web.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            loading.centerXAnchor.constraint(equalTo: content.centerXAnchor),
            loading.centerYAnchor.constraint(equalTo: content.centerYAnchor),
        ])

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1080, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "까치녹음기"
        window.minSize = NSSize(width: 420, height: 560)
        window.contentView = content
        window.delegate = self
        window.isReleasedWhenClosed = false
        window.center()
        window.setFrameAutosaveName("KkachiMainWindow")   // 창 크기·위치 기억
        showWindow()
    }

    func showWindow() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func showPage() {
        if web.url == nil { web.load(URLRequest(url: base)) }
        showWindow()
    }

    // 빨간 닫기 버튼: 창만 숨기고 앱(과 녹음)은 계속
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        window.orderOut(nil)
        return false
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        loading.isHidden = true
        web.isHidden = false
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        // 업데이트로 서버가 잠깐 재시작되는 중일 수 있다 → 잠시 뒤 다시
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
            guard let self else { return }
            self.web.load(URLRequest(url: self.base))
        }
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        webView.reload()
    }

    // MARK: 웹 화면이 요청하는 것들

    // 마이크: 우리 화면(127.0.0.1)에만 허용. 맥 자체 권한 창은 처음 한 번 뜬다.
    @available(macOS 12.0, *)
    func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                 initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType,
                 decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        decisionHandler(origin.host == "127.0.0.1" ? .grant : .deny)
    }

    // 파일 올리기 창
    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = false
        panel.beginSheetModal(for: window) { r in completionHandler(r == .OK ? panel.urls : nil) }
    }

    // 다른 사이트 링크는 기본 브라우저로
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if action.shouldPerformDownload { decisionHandler(.download); return }
        if let url = action.request.url, url.host != "127.0.0.1", url.scheme?.hasPrefix("http") == true {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    // 내보내기(Content-Disposition: attachment)는 다운로드 폴더로 저장
    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let disposition = (response.response as? HTTPURLResponse)?
            .value(forHTTPHeaderField: "Content-Disposition") ?? ""
        decisionHandler(disposition.lowercased().hasPrefix("attachment") || !response.canShowMIMEType ? .download : .allow)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let dir = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask)[0]
        var url = dir.appendingPathComponent(suggestedFilename)
        let stem = url.deletingPathExtension().lastPathComponent, ext = url.pathExtension
        var n = 2
        while FileManager.default.fileExists(atPath: url.path) {
            url = dir.appendingPathComponent("\(stem) (\(n))").appendingPathExtension(ext)
            n += 1
        }
        downloads[download] = url
        completionHandler(url)
    }

    func downloadDidFinish(_ download: WKDownload) {
        if let url = downloads.removeValue(forKey: download) {
            NSWorkspace.shared.activateFileViewerSelecting([url])   // Finder 에서 받은 파일 보여주기
        }
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        downloads.removeValue(forKey: download)
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
        _ = request("POST", "api/app/quit", timeout: 2)
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
        guard let (code, data) = request("GET", "api/health"), code == 200 else { return false }
        return String(data: data, encoding: .utf8)?.contains("\"app\":\"kkachi\"") ?? false
    }

    func portBusy() -> Bool { request("GET", "") != nil }

    func busyReason() -> String? {
        guard let (code, data) = request("GET", "api/app"), code == 200,
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let busy = obj["busy"] as? [String], let first = busy.first else { return nil }
        return first
    }

    // MARK: 메뉴

    @objc func reloadPage() { web.reload() }
    @objc func openInBrowser() { NSWorkspace.shared.open(base) }
    @objc func zoomIn() { web.pageZoom = min(2.0, web.pageZoom + 0.1) }
    @objc func zoomOut() { web.pageZoom = max(0.6, web.pageZoom - 0.1) }
    @objc func zoomReset() { web.pageZoom = 1.0 }
    @objc func showMainWindow() { showWindow() }

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
        func submenu(_ title: String, _ items: [NSMenuItem]) {
            let item = NSMenuItem()
            let m = NSMenu(title: title)
            items.forEach(m.addItem)
            item.submenu = m
            main.addItem(item)
        }
        func item(_ title: String, _ action: Selector, _ key: String, _ mods: NSEvent.ModifierFlags = .command) -> NSMenuItem {
            let i = NSMenuItem(title: title, action: action, keyEquivalent: key)
            i.keyEquivalentModifierMask = mods
            return i
        }
        submenu("까치녹음기", [
            item("까치녹음기 창 보기", #selector(showMainWindow), "0"),
            item("브라우저에서 열기", #selector(openInBrowser), "o", [.command, .shift]),
            .separator(),
            item("까치녹음기 끄기", #selector(NSApplication.terminate(_:)), "q"),
        ])
        // 편집 메뉴가 있어야 글자 입력칸에서 ⌘C/⌘V/⌘A/⌘Z 가 동작한다
        submenu("편집", [
            item("실행 취소", Selector(("undo:")), "z"),
            item("실행 복귀", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            item("오려두기", #selector(NSText.cut(_:)), "x"),
            item("복사하기", #selector(NSText.copy(_:)), "c"),
            item("붙여넣기", #selector(NSText.paste(_:)), "v"),
            item("전체 선택", #selector(NSText.selectAll(_:)), "a"),
        ])
        submenu("보기", [
            item("새로고침", #selector(reloadPage), "r"),
            .separator(),
            item("크게", #selector(zoomIn), "+"),
            item("작게", #selector(zoomOut), "-"),
            item("원래 크기", #selector(zoomReset), "0", [.command, .option]),
        ])
        submenu("윈도우", [
            item("최소화", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("닫기", #selector(NSWindow.performClose(_:)), "w"),
        ])
        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
