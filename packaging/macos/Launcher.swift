import AppKit
import Foundation

final class AppDelegate: NSObject, NSApplicationDelegate {
    var process: Process?
    var window: NSWindow!
    var status: NSTextField!
    var openButton: NSButton!
    var sourceChoice: NSPopUpButton!
    var timer: Timer?
    var endpoint: URL?
    var token = ""
    var source = "offline"
    var ticks = 0
    var stopping = false
    var switching = false
    let root: URL = {
        if let index = CommandLine.arguments.firstIndex(of: "--data-dir"), index + 1 < CommandLine.arguments.count {
            return URL(fileURLWithPath: CommandLine.arguments[index + 1], isDirectory: true)
        }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/OKXWorkbench", isDirectory: true)
    }()

    func applicationDidFinishLaunching(_ notification: Notification) {
        let menu = NSMenu(), appMenu = NSMenu(), item = NSMenuItem()
        appMenu.addItem(withTitle: "退出 OKX 工作台", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        item.submenu = appMenu; menu.addItem(item); NSApp.mainMenu = menu
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 540, height: 340),
                          styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "OKX 策略工作台 · 实盘禁用"
        window.isReleasedWhenClosed = false
        let title = NSTextField(labelWithString: "OKX 策略工作台")
        title.font = NSFont.boldSystemFont(ofSize: 25); title.frame = NSRect(x: 28, y: 282, width: 480, height: 36)
        window.contentView?.addSubview(title)
        let note = NSTextField(wrappingLabelWithString: "内置 Python 和依赖 · 仅模拟环境，实盘禁用\n切换环境会先暂停并重启服务，不平仓或撤单。配置不进入网页或日志。")
        note.frame = NSRect(x: 28, y: 224, width: 480, height: 52); window.contentView?.addSubview(note)
        let sourceLabel = NSTextField(labelWithString: "数据来源")
        sourceLabel.frame = NSRect(x: 28, y: 191, width: 75, height: 25); window.contentView?.addSubview(sourceLabel)
        sourceChoice = NSPopUpButton(frame: NSRect(x: 103, y: 186, width: 225, height: 32), pullsDown: false)
        sourceChoice.addItems(withTitles: ["离线演练（无密钥）", "OKX 模拟盘（真实 API）"])
        sourceChoice.target = self; sourceChoice.action = #selector(changeSource)
        window.contentView?.addSubview(sourceChoice)
        let configure = NSButton(title: "配置模拟盘", target: self, action: #selector(configureDemo))
        configure.frame = NSRect(x: 340, y: 186, width: 165, height: 32); configure.bezelStyle = .rounded
        window.contentView?.addSubview(configure)
        status = NSTextField(wrappingLabelWithString: "正在启动本机服务…")
        status.frame = NSRect(x: 28, y: 85, width: 480, height: 90); window.contentView?.addSubview(status)
        openButton = NSButton(title: "打开工作台", target: self, action: #selector(openBrowser))
        openButton.frame = NSRect(x: 28, y: 30, width: 145, height: 36); openButton.bezelStyle = .rounded; openButton.isEnabled = false
        window.contentView?.addSubview(openButton)
        let quit = NSButton(title: "暂停并退出", target: NSApp, action: #selector(NSApplication.terminate(_:)))
        quit.frame = NSRect(x: 181, y: 30, width: 145, height: 36); quit.bezelStyle = .rounded
        window.contentView?.addSubview(quit)
        window.center(); window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
        if let data = try? Data(contentsOf: root.appendingPathComponent("desktop-preferences.json")),
           let value = (try? JSONSerialization.jsonObject(with: data)) as? [String: String], value["source"] == "okx" {
            source = "okx"; sourceChoice.selectItem(at: 1)
        }
        launchServer()
    }

    func launchServer() {
        endpoint = nil; token = ""; ticks = 0; openButton.isEnabled = false
        status.stringValue = "正在启动 \(source == "okx" ? "OKX 模拟盘" : "离线演练")…"
        do {
            try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            SecureCredentialFile.removeEmptyOrphans(in: root)
            let log = root.appendingPathComponent("desktop.log")
            if !FileManager.default.fileExists(atPath: log.path) {
                FileManager.default.createFile(atPath: log.path, contents: nil, attributes: [.posixPermissions: 0o600])
            }
            let handle = try FileHandle(forWritingTo: log); try handle.seekToEnd()
            let child = Process(); process = child
            child.standardOutput = handle; child.standardError = handle
            child.executableURL = Bundle.main.resourceURL!.appendingPathComponent("server/OKXServer")
            child.arguments = ["--data-dir", root.path, "--source", source]
            child.environment = ["HOME": FileManager.default.homeDirectoryForCurrentUser.path, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"]
            child.terminationHandler = { [weak self] finished in
                DispatchQueue.main.async {
                    guard let self = self, self.process === finished, !self.stopping, !self.switching else { return }
                    if finished.terminationStatus == 0 { NSApp.terminate(nil) }
                }
            }
            try child.run()
            timer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in self?.checkReady() }
        } catch { status.stringValue = "启动失败；请查看用户数据目录内 desktop.log。" }
    }

    func checkReady() {
        ticks += 1
        guard let child = process, child.isRunning else {
            timer?.invalidate(); openButton.isEnabled = false
            status.stringValue = "服务未启动或已退出。检查模拟盘配置、账本占用及 desktop.log。"
            return
        }
        if endpoint == nil, let data = try? Data(contentsOf: root.appendingPathComponent("desktop.json")),
           let info = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
           let pid = info["pid"] as? Int, pid == Int(child.processIdentifier),
           let value = info["url"] as? String, let url = URL(string: value), let secret = info["token"] as? String {
            endpoint = url; token = secret; openButton.isEnabled = true
            status.stringValue = "\(source == "okx" ? "OKX 模拟盘 API" : "离线合成行情") · \(value)\n已暂停启动，须在网页确认策略。凭据只供后端使用。"
            if !CommandLine.arguments.contains("--no-browser") { openBrowser() }
        }
        if endpoint == nil && ticks >= 120 { timer?.invalidate(); status.stringValue = "启动超时，请检查配置及 desktop.log。" }
    }

    @objc func openBrowser() { if let url = endpoint { NSWorkspace.shared.open(url) } }

    func stopServer(completion: @escaping () -> Void) {
        timer?.invalidate()
        guard let child = process, child.isRunning else { completion(); return }
        var completed = false
        let finish = {
            if completed { return }; completed = true
            if child.isRunning { child.terminate(); child.waitUntilExit() }
            if self.process === child { self.process = nil }
            completion()
        }
        guard let url = endpoint?.appendingPathComponent("api/desktop/quit") else { finish(); return }
        var request = URLRequest(url: url); request.httpMethod = "POST"; request.httpBody = Data("{}".utf8)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        URLSession.shared.dataTask(with: request) { _, _, _ in DispatchQueue.main.async { finish() } }.resume()
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) { finish() }
    }

    func restart(_ next: String) {
        if switching || stopping { return }
        switching = true; openButton.isEnabled = false; sourceChoice.isEnabled = false
        status.stringValue = "正在暂停并切换；已有仓位与挂单不会被自动处理…"
        stopServer { [weak self] in
            guard let self = self else { return }
            self.source = next
            let data = try? JSONSerialization.data(withJSONObject: ["source": next])
            try? data?.write(to: self.root.appendingPathComponent("desktop-preferences.json"), options: .atomic)
            self.switching = false; self.sourceChoice.isEnabled = true; self.launchServer()
        }
    }

    @objc func changeSource() {
        let next = sourceChoice.indexOfSelectedItem == 1 ? "okx" : "offline"
        if next == source { return }
        restart(next)
    }

    @objc func configureDemo() {
        if switching || stopping { return }
        switching = true; sourceChoice.isEnabled = false; openButton.isEnabled = false
        stopServer { [weak self] in
            guard let self = self else { return }
            self.switching = false; self.sourceChoice.isEnabled = true
            self.showConfiguration()
        }
    }

    func showConfiguration() {
        let alert = NSAlert(); alert.messageText = "配置 OKX 模拟盘（禁止实盘凭据）"
        alert.informativeText = "仅保存读取+交易、无提现权限的模拟盘凭据至本机用户目录受限文件。保存后暂停服务；选择 OKX 模拟盘才会连接。不会在网页回显。"
        alert.addButton(withTitle: "保存"); alert.addButton(withTitle: "取消")
        let view = NSView(frame: NSRect(x: 0, y: 0, width: 400, height: 135))
        var fields: [NSSecureTextField] = []
        for (index, label) in ["API Key", "Secret Key", "Passphrase"].enumerated() {
            let field = NSSecureTextField(frame: NSRect(x: 0, y: 95 - index * 43, width: 400, height: 30))
            field.placeholderString = label; fields.append(field); view.addSubview(field)
        }
        alert.accessoryView = view
        guard alert.runModal() == .alertFirstButtonReturn else { launchServer(); return }
        let values = fields.map { $0.stringValue }
        guard values.allSatisfy({ !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }) else {
            status.stringValue = "三个凭据字段均不能为空；未保存。服务保持停止。"; return
        }
        do {
            try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            let file = root.appendingPathComponent("demo-credentials.json")
            let data = try JSONSerialization.data(withJSONObject: ["api_key": values[0], "secret_key": values[1], "passphrase": values[2]])
            try SecureCredentialFile.write(data, to: file)
            fields.forEach { $0.stringValue = "" }
            launchServer()
        } catch { status.stringValue = "配置保存失败，服务保持停止；请重新填写配置。未保留明文临时副本。" }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if stopping || process?.isRunning != true { return .terminateNow }
        stopping = true
        stopServer { NSApp.reply(toApplicationShouldTerminate: true) }
        return .terminateLater
    }
}

@main struct DesktopMain {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate; app.setActivationPolicy(.regular)
        withExtendedLifetime(delegate) { app.run() }
    }
}
