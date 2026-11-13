import AppKit
import Foundation

final class AppDelegate: NSObject, NSApplicationDelegate {
    let process = Process()
    var window: NSWindow!
    var status: NSTextField!
    var openButton: NSButton!
    var timer: Timer?
    var endpoint: URL?
    var token = ""
    var ticks = 0
    var stopping = false
    let root: URL = {
        if let index = CommandLine.arguments.firstIndex(of: "--data-dir"),
           index + 1 < CommandLine.arguments.count {
            return URL(fileURLWithPath: CommandLine.arguments[index + 1], isDirectory: true)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/OKXWorkbench", isDirectory: true)
    }()

    func applicationDidFinishLaunching(_ notification: Notification) {
        let menu = NSMenu()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "退出 OKX 工作台", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let item = NSMenuItem()
        item.submenu = appMenu
        menu.addItem(item)
        NSApp.mainMenu = menu
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 510, height: 260),
                          styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "OKX 策略工作台 · 离线演练"
        window.isReleasedWhenClosed = false
        let title = NSTextField(labelWithString: "OKX 策略工作台")
        title.font = NSFont.boldSystemFont(ofSize: 25)
        title.frame = NSRect(x: 28, y: 199, width: 450, height: 36)
        window.contentView?.addSubview(title)
        let note = NSTextField(wrappingLabelWithString:
            "无密钥离线演练 · 模拟环境 · 实盘禁用\n内置 Python 和依赖，无需安装开发工具。关闭窗口会暂停并退出服务。")
        note.frame = NSRect(x: 28, y: 134, width: 450, height: 57)
        window.contentView?.addSubview(note)
        status = NSTextField(wrappingLabelWithString: "正在启动本机服务…")
        status.frame = NSRect(x: 28, y: 81, width: 450, height: 46)
        window.contentView?.addSubview(status)
        openButton = NSButton(title: "打开工作台", target: self, action: #selector(openBrowser))
        openButton.frame = NSRect(x: 28, y: 24, width: 145, height: 36)
        openButton.bezelStyle = .rounded
        openButton.isEnabled = false
        window.contentView?.addSubview(openButton)
        let quit = NSButton(title: "暂停并退出", target: NSApp, action: #selector(NSApplication.terminate(_:)))
        quit.frame = NSRect(x: 181, y: 24, width: 145, height: 36)
        quit.bezelStyle = .rounded
        window.contentView?.addSubview(quit)
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        do {
            try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
            let log = root.appendingPathComponent("desktop.log")
            if !FileManager.default.fileExists(atPath: log.path) {
                FileManager.default.createFile(atPath: log.path, contents: nil,
                                               attributes: [.posixPermissions: 0o600])
            }
            let handle = try FileHandle(forWritingTo: log)
            try handle.seekToEnd()
            process.standardOutput = handle
            process.standardError = handle
            process.executableURL = Bundle.main.resourceURL!.appendingPathComponent("server/OKXServer")
            process.arguments = ["--data-dir", root.path]
            process.environment = ["HOME": FileManager.default.homeDirectoryForCurrentUser.path,
                                   "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"]
            process.terminationHandler = { [weak self] child in
                DispatchQueue.main.async {
                    guard let self = self, !self.stopping else { return }
                    if child.terminationStatus == 0 { NSApp.terminate(nil) }
                }
            }
            try process.run()
            timer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in self?.checkReady() }
        } catch {
            status.stringValue = "启动失败；请查看用户数据目录内 desktop.log。"
        }
    }

    func checkReady() {
        ticks += 1
        if !process.isRunning {
            timer?.invalidate()
            status.stringValue = "服务未启动或已退出。可能已有实例使用账本；请查看 desktop.log。"
            openButton.isEnabled = false
            return
        }
        if endpoint == nil,
           let data = try? Data(contentsOf: root.appendingPathComponent("desktop.json")),
           let info = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
           let pid = info["pid"] as? Int, pid == Int(process.processIdentifier),
           let value = info["url"] as? String, let url = URL(string: value),
           let secret = info["token"] as? String {
            endpoint = url
            token = secret
            status.stringValue = "本机服务已启动：\(value)\n仅离线行情与本地演练账本，不连接 OKX。"
            openButton.isEnabled = true
            if !CommandLine.arguments.contains("--no-browser") { openBrowser() }
        }
        if endpoint == nil && ticks >= 120 {
            timer?.invalidate()
            status.stringValue = "启动超时；请查看用户数据目录内 desktop.log。"
        }
    }

    @objc func openBrowser() {
        if let url = endpoint { NSWorkspace.shared.open(url) }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if stopping || !process.isRunning { return .terminateNow }
        stopping = true
        timer?.invalidate()
        if let url = endpoint?.appendingPathComponent("api/desktop/quit") {
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.httpBody = Data("{}".utf8)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            let task = URLSession.shared.dataTask(with: request) { [weak self] _, _, _ in
                DispatchQueue.main.async { self?.finishQuit() }
            }
            task.resume()
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in self?.finishQuit() }
            return .terminateLater
        }
        process.terminate()
        process.waitUntilExit()
        return .terminateNow
    }

    func finishQuit() {
        if process.isRunning { process.terminate(); process.waitUntilExit() }
        NSApp.reply(toApplicationShouldTerminate: true)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
