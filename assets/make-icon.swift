import AppKit

let S: CGFloat = 1024
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(S), pixelsHigh: Int(S), bitsPerSample: 8,
                           samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                           bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)

// macOS 아이콘 격자: 824px 둥근 사각형 + 그림자
let rect = NSRect(x: 100, y: 100, width: 824, height: 824)
let shape = NSBezierPath(roundedRect: rect, xRadius: 185, yRadius: 185)
let shadow = NSShadow()
shadow.shadowColor = NSColor.black.withAlphaComponent(0.28)
shadow.shadowBlurRadius = 28
shadow.shadowOffset = NSSize(width: 0, height: -12)
NSGraphicsContext.current!.saveGraphicsState()
shadow.set()
NSColor.white.setFill()
shape.fill()
NSGraphicsContext.current!.restoreGraphicsState()
NSGradient(starting: NSColor(calibratedRed: 0.98, green: 0.97, blue: 0.95, alpha: 1),
           ending: NSColor(calibratedRed: 0.90, green: 0.93, blue: 0.99, alpha: 1))!.draw(in: shape, angle: -90)

// 까치 날개의 푸른빛 원
NSColor(calibratedRed: 0.184, green: 0.357, blue: 0.827, alpha: 1).setFill()
NSBezierPath(ovalIn: NSRect(x: 212, y: 170, width: 600, height: 600)).fill()

// 검은 새 이모지
let emoji = "🐦‍⬛" as NSString
let font = NSFont(name: "Apple Color Emoji", size: 380)!
let attrs: [NSAttributedString.Key: Any] = [.font: font]
let sz = emoji.size(withAttributes: attrs)
emoji.draw(at: NSPoint(x: (S - sz.width) / 2, y: 470 - sz.height / 2), withAttributes: attrs)

// 녹음 중 빨간 점
NSColor.white.setFill()
NSBezierPath(ovalIn: NSRect(x: 676, y: 676, width: 150, height: 150)).fill()
NSColor(calibratedRed: 0.84, green: 0.23, blue: 0.18, alpha: 1).setFill()
NSBezierPath(ovalIn: NSRect(x: 690, y: 690, width: 122, height: 122)).fill()

NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
