// 다른 앱 소리 녹음 (ScreenCaptureKit).
//
// 웹 화면(WKWebView)에서는 다른 앱 소리를 가져올 수 없어서 실행기가 직접 받는다.
// - 고른 앱(또는 맥 전체)의 소리와, 원하면 마이크(macOS 15+)를 따로 받는다.
// - 둘 다 16kHz 모노 16비트 PCM 으로 바꿔서 10초마다 서버의 /api/live/<id>/chunks/<n>?track=app|mic 로 보낸다.
//   (브라우저 녹음과 같은 구조라 창을 닫거나 앱이 꺼져도 그때까지는 남고, 서버가 끝날 때 섞어서 합친다)
// - 소리가 안 나는 동안 ScreenCaptureKit 이 아무것도 안 보내는 경우가 있어서, 시계 기준으로
//   비는 만큼 무음을 채워 넣는다. 그래야 앱 소리와 마이크가 시간이 어긋나지 않는다.

import AVFoundation
import AppKit
import CoreMedia
import ScreenCaptureKit

final class AudioCapture: NSObject, SCStreamOutput, SCStreamDelegate {
    static let rate: Double = 16000
    static let chunkSeconds: Double = 10

    let recId: String
    let base: URL
    let appName: String
    let withMic: Bool
    /// 녹음 중 화면에 보낼 것 (소리 크기, 저장된 시간, 끊김) → 메인 스레드에서 불림
    var onLevel: (([String: Any]) -> Void)?
    /// 녹음이 맥 쪽 사정으로 멈췄을 때 (권한 회수 등)
    var onEnded: ((String) -> Void)?

    private let q = DispatchQueue(label: "kkachi.capture")
    private let uq = DispatchQueue(label: "kkachi.capture.upload")
    private var stream: SCStream?
    private var tracks: [String: Track] = [:]
    private var startedAt: TimeInterval = 0
    private var pausedAt: TimeInterval?
    private var pausedTotal: TimeInterval = 0
    private var uploads: [(track: String, seq: Int, data: Data)] = []
    private var uploading = false
    private var savedSamples: Int64 = 0
    private var offline = false
    private var gone = false             // 서버에서 녹음이 끝났거나 지워짐 → 더 보내지 않음
    private var timers: [DispatchSourceTimer] = []
    private(set) var stopped = false

    init(recId: String, base: URL, appName: String, withMic: Bool) {
        self.recId = recId
        self.base = base
        self.appName = appName
        self.withMic = withMic
    }

    static var micSupported: Bool {
        if #available(macOS 15.0, *) { return true }
        return false
    }

    /// 녹음할 수 있는 앱 목록 (Dock 에 보이는 앱들, 아이콘 포함). 권한 없이도 볼 수 있다.
    static func appList() -> [[String: String]] {
        let me = Bundle.main.bundleIdentifier
        var seen = Set<String>()
        return NSWorkspace.shared.runningApplications
            .filter { $0.activationPolicy == .regular && $0.bundleIdentifier != nil && $0.bundleIdentifier != me }
            .compactMap { app -> [String: String]? in
                guard let id = app.bundleIdentifier, seen.insert(id).inserted else { return nil }
                var item = ["bundle": id, "name": app.localizedName ?? id]
                if let icon = app.icon.flatMap(iconURL) { item["icon"] = icon }
                return item
            }
            .sorted { $0["name"]!.localizedCompare($1["name"]!) == .orderedAscending }
    }

    /// 앱 아이콘 → 화면에 바로 넣을 수 있는 작은 PNG (data: 주소)
    private static func iconURL(_ icon: NSImage) -> String? {
        let px = 64
        guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8,
                                         samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                         colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { return nil }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
        icon.draw(in: NSRect(x: 0, y: 0, width: px, height: px))
        NSGraphicsContext.restoreGraphicsState()
        guard let png = rep.representation(using: .png, properties: [:]) else { return nil }
        return "data:image/png;base64," + png.base64EncodedString()
    }

    // MARK: 시작 / 일시정지 / 끝

    /// bundle 이 nil 이면 맥 전체 소리 (이 앱 소리는 뺌)
    func start(bundle: String?) async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        guard let display = content.displays.first else { throw CaptureError("화면 정보를 찾을 수 없어요.") }
        let filter: SCContentFilter
        if let bundle {
            let apps = content.applications.filter { $0.bundleIdentifier == bundle }
            guard !apps.isEmpty else { throw CaptureError("'\(appName)' 앱이 꺼져 있어요. 앱을 켠 뒤 다시 시도해 주세요.") }
            filter = SCContentFilter(display: display, including: apps, exceptingWindows: [])
        } else {
            let me = content.applications.filter { $0.bundleIdentifier == Bundle.main.bundleIdentifier }
            filter = SCContentFilter(display: display, excludingApplications: me, exceptingWindows: [])
        }

        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.excludesCurrentProcessAudio = true
        config.sampleRate = Int(Self.rate)
        config.channelCount = 1
        // 화면은 필요 없지만 꺼둘 수는 없어서 가장 작고 느리게
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        config.queueDepth = 3
        if withMic, #available(macOS 15.0, *) {
            config.captureMicrophone = true
        }

        let s = SCStream(filter: filter, configuration: config, delegate: self)
        try s.addStreamOutput(self, type: .screen, sampleHandlerQueue: q)
        try s.addStreamOutput(self, type: .audio, sampleHandlerQueue: q)
        if withMic, #available(macOS 15.0, *) {
            try s.addStreamOutput(self, type: .microphone, sampleHandlerQueue: q)
        }
        q.sync {
            tracks["app"] = Track(name: "app")
            if withMic && Self.micSupported { tracks["mic"] = Track(name: "mic") }
            startedAt = ProcessInfo.processInfo.systemUptime
        }
        try await s.startCapture()
        stream = s
        q.sync {
            timers.append(repeating(Self.chunkSeconds) { [weak self] in self?.flush() })
            timers.append(repeating(0.25) { [weak self] in self?.reportLevel() })
        }
    }

    func setPaused(_ paused: Bool) {
        q.async {
            let now = ProcessInfo.processInfo.systemUptime
            if paused, self.pausedAt == nil {
                self.pausedAt = now
            } else if !paused, let at = self.pausedAt {
                self.pausedTotal += now - at
                self.pausedAt = nil
            }
        }
    }

    var status: [String: Any] {
        q.sync {
            ["id": recId, "app": appName, "mic": tracks["mic"] != nil,
             "elapsed": activeTime() * 1000, "paused": pausedAt != nil]
        }
    }

    /// 녹음을 멈추고, 남은 조각을 서버에 다 보낼 때까지 기다린다 (최대 timeout 초)
    func stop(timeout: TimeInterval = 60, done: @escaping () -> Void) {
        let s = stream
        stream = nil
        s?.stopCapture { _ in }
        q.async {
            if !self.stopped {
                self.stopped = true
                self.timers.forEach { $0.cancel() }
                self.timers = []
                self.flush()
            }
            self.waitForUploads(until: ProcessInfo.processInfo.systemUptime + timeout, done: done)
        }
    }

    private func waitForUploads(until deadline: TimeInterval, done: @escaping () -> Void) {
        if (uploads.isEmpty && !uploading) || gone || ProcessInfo.processInfo.systemUptime > deadline {
            DispatchQueue.main.async(execute: done)
            return
        }
        q.asyncAfter(deadline: .now() + 0.2) { self.waitForUploads(until: deadline, done: done) }
    }

    // MARK: SCStream

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard !stopped, pausedAt == nil, sb.isValid else { return }
        let name: String
        switch type {
        case .audio: name = "app"
        case .screen: return
        default: name = "mic"  // .microphone (macOS 15+)
        }
        guard let track = tracks[name], let samples = track.convert(sb), !samples.isEmpty else { return }
        // 시계보다 0.5초 넘게 모자라면 그동안 소리가 안 왔던 것 → 무음으로 채운다
        let expectedStart = Int64(activeTime() * Self.rate) - Int64(samples.count)
        if expectedStart - track.written > Int64(Self.rate / 2) {
            track.pad(Int(expectedStart - track.written))
        }
        track.append(samples)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        let message = "녹음이 멈췄어요 (\(error.localizedDescription)). 지금까지 녹음된 부분은 저장돼 있어요."
        stop { [weak self] in self?.onEnded?(message) }
    }

    // MARK: 조각 보내기

    private func activeTime() -> TimeInterval {
        let now = pausedAt ?? ProcessInfo.processInfo.systemUptime
        return max(0, now - startedAt - pausedTotal)
    }

    private func flush() {
        let target = Int64(activeTime() * Self.rate)
        for name in ["app", "mic"] {
            guard let t = tracks[name] else { continue }
            // 마지막 소리 이후 1초 넘게 조용했으면 지금까지 무음으로 채운다 (도착 중인 소리 몫 0.3초는 남겨둠)
            if target - t.written > Int64(Self.rate) {
                t.pad(Int(target - t.written - Int64(Self.rate * 0.3)))
            }
            guard !t.pending.isEmpty else { continue }
            uploads.append((name, t.seq, t.pending))
            t.seq += 1
            t.pending = Data()
        }
        if !uploading && !uploads.isEmpty {
            uploading = true
            uq.async { self.pump() }
        }
    }

    private func pump() {
        while true {
            guard let item = q.sync(execute: { () -> (track: String, seq: Int, data: Data)? in
                if uploads.isEmpty || gone { uploading = false; return nil }
                return uploads.first
            }) else { return }
            let code = put(item.track, item.seq, item.data)
            if code == 204 || code == 200 {
                q.sync {
                    uploads.removeFirst()
                    offline = false
                    if item.track == "app" { savedSamples += Int64(item.data.count / 2) }
                }
            } else if code == 404 || code == 409 {
                q.sync { uploads.removeAll(); gone = true; uploading = false }
                return
            } else {
                // 앱이 업데이트로 잠깐 재시작 중이거나 바쁨 → 들고 있다가 다시 보낸다
                q.sync { offline = true }
                Thread.sleep(forTimeInterval: 3)
            }
        }
    }

    private func put(_ track: String, _ seq: Int, _ data: Data) -> Int {
        var comps = URLComponents(url: base.appendingPathComponent("api/live/\(recId)/chunks/\(seq)"),
                                  resolvingAgainstBaseURL: false)!
        comps.queryItems = [URLQueryItem(name: "track", value: track)]
        var req = URLRequest(url: comps.url!)
        req.httpMethod = "PUT"
        req.httpBody = data
        req.timeoutInterval = 30
        let sem = DispatchSemaphore(value: 0)
        var code = 0
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 35)
        return code
    }

    private func reportLevel() {
        let db = tracks.values.map { $0.takeLevel() }.max() ?? -100
        let info: [String: Any] = [
            "db": pausedAt == nil ? db : -100,
            "saved": Double(savedSamples) / Self.rate,
            "offline": offline,
        ]
        DispatchQueue.main.async { self.onLevel?(info) }
    }

    private func repeating(_ interval: Double, _ block: @escaping () -> Void) -> DispatchSourceTimer {
        let t = DispatchSource.makeTimerSource(queue: q)
        t.schedule(deadline: .now() + interval, repeating: interval)
        t.setEventHandler(handler: block)
        t.resume()
        return t
    }
}

struct CaptureError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

/// 트랙 하나(앱 소리 또는 마이크): 들어온 소리를 16kHz 모노 16비트로 바꿔 모아둔다
final class Track {
    let name: String
    var pending = Data()
    var written: Int64 = 0     // 지금까지 쌓은 샘플 수 (시간 맞추기용)
    var seq = 0
    private var converter: AVAudioConverter?
    private var inFormat: AVAudioFormat?
    private var peakSquare: Float = 0
    private let outFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: AudioCapture.rate,
                                          channels: 1, interleaved: true)!

    init(name: String) { self.name = name }

    func convert(_ sb: CMSampleBuffer) -> [Int16]? {
        guard let desc = CMSampleBufferGetFormatDescription(sb),
              let asbdPtr = CMAudioFormatDescriptionGetStreamBasicDescription(desc) else { return nil }
        var asbd = asbdPtr.pointee
        guard let fmt = AVAudioFormat(streamDescription: &asbd) else { return nil }
        let frames = AVAudioFrameCount(CMSampleBufferGetNumSamples(sb))
        guard frames > 0, let input = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: frames) else { return nil }
        input.frameLength = frames
        guard CMSampleBufferCopyPCMDataIntoAudioBufferList(
            sb, at: 0, frameCount: Int32(frames), into: input.mutableAudioBufferList) == noErr else { return nil }

        if converter == nil || inFormat != fmt {
            converter = AVAudioConverter(from: fmt, to: outFormat)
            converter?.downmix = true
            inFormat = fmt
        }
        guard let converter else { return nil }
        let capacity = AVAudioFrameCount(Double(frames) * AudioCapture.rate / fmt.sampleRate) + 64
        guard let output = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: capacity) else { return nil }
        var fed = false
        var error: NSError?
        converter.convert(to: output, error: &error) { _, status in
            if fed { status.pointee = .noDataNow; return nil }
            fed = true
            status.pointee = .haveData
            return input
        }
        guard error == nil, let ch = output.int16ChannelData else { return nil }
        return Array(UnsafeBufferPointer(start: ch[0], count: Int(output.frameLength)))
    }

    func append(_ samples: [Int16]) {
        var sum: Float = 0
        for s in samples {
            let v = Float(s) / 32768
            sum += v * v
        }
        peakSquare = max(peakSquare, sum / Float(max(1, samples.count)))
        samples.withUnsafeBufferPointer { pending.append(Data(buffer: $0)) }
        written += Int64(samples.count)
    }

    func pad(_ count: Int) {
        guard count > 0 else { return }
        let n = min(count, Int(AudioCapture.rate) * 60 * 30)  // 이상한 값이어도 30분 넘게는 안 채움
        pending.append(Data(count: n * 2))
        written += Int64(n)
    }

    /// 마지막으로 물어본 뒤 가장 컸던 소리 크기 (dB)
    func takeLevel() -> Double {
        let v = peakSquare
        peakSquare = 0
        return 10 * log10(Double(v) + 1e-10)
    }
}
