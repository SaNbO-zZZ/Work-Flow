// 用法: swift websnap.swift <网址或文件路径> <输出.png> [宽=1440] [高=900] [深色=0/1]
// 离屏 WKWebView 截图：M2 核对界面、M4 做网页截图叠层都用它，不需要屏幕录制权限
import AppKit
import WebKit

let args = CommandLine.arguments
guard args.count >= 3 else { print("用法: websnap <url|path> <out.png> [w] [h] [dark]"); exit(2) }
let target = args[1], out = args[2]
let width = args.count > 3 ? Double(args[3]) ?? 1440 : 1440
let height = args.count > 4 ? Double(args[4]) ?? 900 : 900
let dark = args.count > 5 && args[5] == "1"

let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
if dark { app.appearance = NSAppearance(named: .darkAqua) } else { app.appearance = NSAppearance(named: .aqua) }

final class Snap: NSObject, WKNavigationDelegate {
    let web: WKWebView
    let window: NSWindow
    init(w: Double, h: Double) {
        let frame = NSRect(x: 0, y: 0, width: w, height: h)
        web = WKWebView(frame: frame, configuration: WKWebViewConfiguration())
        window = NSWindow(contentRect: frame, styleMask: [.borderless], backing: .buffered, defer: false)
        super.init()
        window.contentView = web
        web.navigationDelegate = self
        if ProcessInfo.processInfo.environment["WEBSNAP_TRANSPARENT"] == "1" {
            web.setValue(false, forKey: "drawsBackground")
        }
    }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
            webView.takeSnapshot(with: nil) { image, error in
                guard let image, let tiff = image.tiffRepresentation,
                      let png = NSBitmapImageRep(data: tiff)?.representation(using: .png, properties: [:]) else {
                    print("截图失败: \(String(describing: error))"); exit(1)
                }
                do { try png.write(to: URL(fileURLWithPath: out)); print("已保存 \(out)"); exit(0) }
                catch { print("写文件失败: \(error)"); exit(1) }
            }
        }
    }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { print("加载失败: \(error)"); exit(1) }
}

let snap = Snap(w: width, h: height)
if target.hasPrefix("http") {
    snap.web.load(URLRequest(url: URL(string: target)!))
} else {
    let url = URL(fileURLWithPath: target)
    snap.web.loadFileURL(url, allowingReadAccessTo: url.deletingLastPathComponent())
}
DispatchQueue.main.asyncAfter(deadline: .now() + 30) { print("超时"); exit(1) }
app.run()
