// 本机界面操作小工具（F1 Harness 拉起，系统权限算在 F1 Harness 名下）。每条命令输出一行 JSON。
// hermes-ui perm [--request]            查询/申请"辅助功能"和"屏幕录制"权限
// hermes-ui activate <bundle>           把应用切到前台
// hermes-ui windows <bundle> [--all]    列出应用的窗口（id、标题、层级、位置大小）；--all 含浮层窗口
// hermes-ui front                       当前前台应用的 bundle id
// hermes-ui menu <bundle> <菜单> <项> 按菜单路径点菜单项，如 menu com.lemon.lvpro 文件 新建草稿
// hermes-ui shot <bundle> <out.png>     截应用最大的那个窗口
// hermes-ui ax <bundle> <out.json> [--depth N]   导出辅助功能界面树
// hermes-ui click <x> <y> [--double|--right]     屏幕坐标点击（左上角为原点）
// hermes-ui drag <x1> <y1> <x2> <y2>    按住左键拖动（框选）
// hermes-ui key <组合>                  如 esc、return、cmd+e、cmd+shift+e
// hermes-ui type <文字>                 逐字输入
// hermes-ui fit <bundle>                把应用的窗口整个挪进主屏幕可见区域（放不下就缩小）
import AppKit
import ApplicationServices
import CoreGraphics
import Foundation

func emit(_ obj: [String: Any]) {
    let data = try! JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])
    print(String(data: data, encoding: .utf8)!)
}

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(2)
}

func app(_ bundle: String) -> NSRunningApplication {
    let all = NSRunningApplication.runningApplications(withBundleIdentifier: bundle)
    guard let a = all.first(where: { $0.activationPolicy == .regular }) ?? all.first else { fail("应用没在运行：\(bundle)") }
    return a
}

func windows(_ pid: pid_t, all: Bool = false) -> [[String: Any]] {
    let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
    return list.filter {
        guard ($0[kCGWindowOwnerPID as String] as? pid_t) == pid else { return false }
        let layer = $0[kCGWindowLayer as String] as? Int ?? 0
        return all ? layer < 20 : layer == 0
    }
        .map { w in
            let b = w[kCGWindowBounds as String] as? [String: Any] ?? [:]
            return ["id": w[kCGWindowNumber as String] ?? 0, "title": w[kCGWindowName as String] ?? "",
                    "layer": w[kCGWindowLayer as String] ?? 0,
                    "x": b["X"] ?? 0, "y": b["Y"] ?? 0, "w": b["Width"] ?? 0, "h": b["Height"] ?? 0]
        }
}

func area(_ w: [String: Any]) -> Double { (w["w"] as? Double ?? 0) * (w["h"] as? Double ?? 0) }

func attr(_ el: AXUIElement, _ name: String) -> AnyObject? {
    var v: AnyObject?
    return AXUIElementCopyAttributeValue(el, name as CFString, &v) == .success ? v : nil
}

func frame(_ el: AXUIElement) -> [String: Double]? {
    guard let p = attr(el, kAXPositionAttribute), let s = attr(el, kAXSizeAttribute) else { return nil }
    var pt = CGPoint.zero, sz = CGSize.zero
    AXValueGetValue(p as! AXValue, .cgPoint, &pt)
    AXValueGetValue(s as! AXValue, .cgSize, &sz)
    return ["x": pt.x, "y": pt.y, "w": sz.width, "h": sz.height]
}

func dump(_ el: AXUIElement, _ depth: Int, _ maxDepth: Int) -> [String: Any] {
    var node: [String: Any] = [:]
    for (key, name) in [("role", kAXRoleAttribute), ("subrole", kAXSubroleAttribute), ("title", kAXTitleAttribute),
                        ("desc", kAXDescriptionAttribute), ("id", kAXIdentifierAttribute), ("help", kAXHelpAttribute)] {
        if let s = attr(el, name) as? String, !s.isEmpty { node[key] = s }
    }
    if let v = attr(el, kAXValueAttribute) {
        if let s = v as? String, !s.isEmpty { node["value"] = String(s.prefix(200)) }
        else if let n = v as? NSNumber { node["value"] = n }
    }
    if let f = frame(el) { node["frame"] = f }
    if depth < maxDepth, let kids = attr(el, kAXChildrenAttribute) as? [AXUIElement], !kids.isEmpty {
        node["children"] = kids.map { dump($0, depth + 1, maxDepth) }
    }
    return node
}

let keyCodes: [String: CGKeyCode] = [
    "return": 36, "tab": 48, "space": 49, "delete": 51, "esc": 53, "left": 123, "right": 124, "down": 125, "up": 126,
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14,
    "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26, "8": 28, "0": 29,
    "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46,
]

func post(_ e: CGEvent?) {
    e?.post(tap: .cghidEventTap)
    usleep(15_000)
}

let args = Array(CommandLine.arguments.dropFirst())
func arg(_ i: Int) -> String {
    if i >= args.count { fail("参数不够：\(args)") }
    return args[i]
}

switch args.first {
case "perm":
    let request = args.contains("--request")
    let ax = request
        ? AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)
        : AXIsProcessTrusted()
    var screen = CGPreflightScreenCaptureAccess()
    if request && !screen {
        screen = CGRequestScreenCaptureAccess()
    }
    emit(["accessibility": ax, "screen": screen])
case "activate":
    let a = app(arg(1))
    emit(["ok": a.activate(options: [.activateAllWindows]), "pid": a.processIdentifier])
case "windows":
    emit(["windows": windows(app(arg(1)).processIdentifier, all: args.contains("--all"))])
case "front":
    emit(["bundle": NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? ""])
case "menu":
    let root = AXUIElementCreateApplication(app(arg(1)).processIdentifier)
    guard var node = attr(root, kAXMenuBarAttribute).map({ $0 as! AXUIElement }) else { fail("读不到菜单栏") }
    for name in args.dropFirst(2) {
        // 菜单栏项→AXMenu→菜单项：逐层找标题相等的子元素，跳过中间的 AXMenu
        func child(_ el: AXUIElement) -> AXUIElement? {
            for k in attr(el, kAXChildrenAttribute) as? [AXUIElement] ?? [] {
                if (attr(k, kAXTitleAttribute) as? String) == name { return k }
                if (attr(k, kAXRoleAttribute) as? String) == "AXMenu", let hit = child(k) { return hit }
            }
            return nil
        }
        guard let next = child(node) else { fail("没有菜单项：\(name)") }
        node = next
    }
    emit(["ok": AXUIElementPerformAction(node, kAXPressAction as CFString) == .success])
case "shot":
    guard let main = windows(app(arg(1)).processIdentifier).max(by: { area($0) < area($1) }) else { fail("应用没有可见窗口") }
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
    p.arguments = ["-x", "-o", "-l", "\(main["id"]!)", arg(2)]
    try! p.run()
    p.waitUntilExit()
    var out = main
    out["ok"] = p.terminationStatus == 0
    out["path"] = arg(2)
    emit(out)
case "ax":
    let a = app(arg(1))
    let depth = args.firstIndex(of: "--depth").map { Int(arg($0 + 1)) ?? 30 } ?? 30
    let root = AXUIElementCreateApplication(a.processIdentifier)
    // Qt/Chromium 类应用要打开这两个开关才会暴露界面树
    AXUIElementSetAttributeValue(root, "AXManualAccessibility" as CFString, kCFBooleanTrue)
    AXUIElementSetAttributeValue(root, "AXEnhancedUserInterface" as CFString, kCFBooleanTrue)
    usleep(300_000)
    let data = try! JSONSerialization.data(withJSONObject: dump(root, 0, depth), options: [.prettyPrinted, .sortedKeys])
    try! data.write(to: URL(fileURLWithPath: arg(2)))
    emit(["ok": true, "path": arg(2)])
case "click":
    guard let x = Double(arg(1)), let y = Double(arg(2)) else { fail("坐标不对") }
    let pt = CGPoint(x: x, y: y)
    let right = args.contains("--right")
    let down: CGEventType = right ? .rightMouseDown : .leftMouseDown
    let up: CGEventType = right ? .rightMouseUp : .leftMouseUp
    let btn: CGMouseButton = right ? .right : .left
    post(CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: pt, mouseButton: .left))
    usleep(80_000)
    for n in 1...(args.contains("--double") ? 2 : 1) {
        let d = CGEvent(mouseEventSource: nil, mouseType: down, mouseCursorPosition: pt, mouseButton: btn)
        let u = CGEvent(mouseEventSource: nil, mouseType: up, mouseCursorPosition: pt, mouseButton: btn)
        d?.setIntegerValueField(.mouseEventClickState, value: Int64(n))
        u?.setIntegerValueField(.mouseEventClickState, value: Int64(n))
        post(d)
        post(u)
    }
    emit(["ok": true, "x": x, "y": y])
case "drag":
    guard let x1 = Double(arg(1)), let y1 = Double(arg(2)), let x2 = Double(arg(3)), let y2 = Double(arg(4)) else { fail("坐标不对") }
    let a = CGPoint(x: x1, y: y1), b = CGPoint(x: x2, y: y2)
    post(CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: a, mouseButton: .left))
    usleep(80_000)
    post(CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: a, mouseButton: .left))
    for i in 1...20 {
        let t = Double(i) / 20
        post(CGEvent(mouseEventSource: nil, mouseType: .leftMouseDragged,
                     mouseCursorPosition: CGPoint(x: x1 + (x2 - x1) * t, y: y1 + (y2 - y1) * t), mouseButton: .left))
        usleep(15_000)
    }
    post(CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: b, mouseButton: .left))
    emit(["ok": true])
case "key":
    let parts = arg(1).lowercased().split(separator: "+").map(String.init)
    guard let code = keyCodes[parts.last!] else { fail("不认识的键：\(parts.last!)") }
    var flags: CGEventFlags = []
    for m in parts.dropLast() {
        switch m {
        case "cmd": flags.insert(.maskCommand)
        case "shift": flags.insert(.maskShift)
        case "alt", "option": flags.insert(.maskAlternate)
        case "ctrl": flags.insert(.maskControl)
        default: fail("不认识的修饰键：\(m)")
        }
    }
    for isDown in [true, false] {
        let e = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: isDown)
        e?.flags = flags
        post(e)
    }
    emit(["ok": true, "key": arg(1)])
case "type":
    for ch in arg(1).utf16 {
        for isDown in [true, false] {
            let e = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: isDown)
            var c = ch
            e?.keyboardSetUnicodeString(stringLength: 1, unicodeString: &c)
            post(e)
        }
    }
    emit(["ok": true, "chars": arg(1).count])
case "fit":
    // 窗口一部分在屏幕外时，弹出的确认框可能落在看不见的地方，点击和截图都会失败
    let root = AXUIElementCreateApplication(app(arg(1)).processIdentifier)
    guard let main = NSScreen.screens.first else { fail("没有屏幕") }
    // Cocoa 坐标原点在主屏左下角，辅助功能坐标原点在主屏左上角
    func axRect(_ s: NSScreen) -> CGRect {
        CGRect(x: s.visibleFrame.minX, y: main.frame.height - s.visibleFrame.maxY, width: s.visibleFrame.width, height: s.visibleFrame.height)
    }
    let area = axRect(main)
    let screens = NSScreen.screens.map(axRect)
    var moved: [[String: Any]] = []
    for w in attr(root, kAXWindowsAttribute) as? [AXUIElement] ?? [] {
        guard let f = frame(w), let fw = f["w"], let fh = f["h"], fw > 50, fh > 50 else { continue }
        let r = CGRect(x: f["x"]!, y: f["y"]!, width: fw, height: fh)
        if screens.contains(where: { $0.contains(r) }) { continue }  // 已完整显示在某块屏幕上就不动
        var sz = CGSize(width: min(fw, area.width), height: min(fh, area.height))
        var pt = CGPoint(x: area.minX + (area.width - sz.width) / 2, y: area.minY + (area.height - sz.height) / 2)
        // 先挪再改大小再挪一次：有的窗口在屏幕边缘时改不了大小
        AXUIElementSetAttributeValue(w, kAXPositionAttribute as CFString, AXValueCreate(.cgPoint, &pt)!)
        AXUIElementSetAttributeValue(w, kAXSizeAttribute as CFString, AXValueCreate(.cgSize, &sz)!)
        AXUIElementSetAttributeValue(w, kAXPositionAttribute as CFString, AXValueCreate(.cgPoint, &pt)!)
        moved.append(["title": attr(w, kAXTitleAttribute) as? String ?? "", "from": f, "to": frame(w) ?? [:]])
    }
    emit(["ok": true, "moved": moved, "screen": ["x": area.minX, "y": area.minY, "w": area.width, "h": area.height]])
default:
    fail("用法见文件开头注释：perm / activate / windows / shot / ax / click / key / type / fit")
}
