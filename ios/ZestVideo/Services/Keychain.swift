import Foundation
import Security

/// The device token lives here and nowhere else. It survives app updates,
/// stays on this phone only, and can be read after the first unlock so
/// background uploads still work.
enum Keychain {
    private static let service = "com.zestlawnpest.video"

    static let deviceTokenKey = "deviceToken"

    @discardableResult
    static func set(_ value: String, for key: String) -> Bool {
        remove(key)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
            kSecValueData as String: Data(value.utf8),
        ]
        let stored = SecItemAdd(query as CFDictionary, nil) == errSecSuccess
        #if targetEnvironment(simulator)
        // A simulator build that is not code-signed has no keychain access.
        // Keep the value in the app's defaults there instead, so the app
        // (and its tests) still stays signed in across a relaunch. Never on
        // a real phone: this branch is compiled out.
        if !stored {
            UserDefaults.standard.set(value, forKey: simulatorPrefix + key)
            return true
        }
        #endif
        return stored
    }

    #if targetEnvironment(simulator)
    private static let simulatorPrefix = "simulator-keychain."
    #endif

    static func get(_ key: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: AnyObject?
        if SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
           let data = result as? Data {
            return String(data: data, encoding: .utf8)
        }
        #if targetEnvironment(simulator)
        return UserDefaults.standard.string(forKey: simulatorPrefix + key)
        #else
        return nil
        #endif
    }

    static func remove(_ key: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
        ]
        SecItemDelete(query as CFDictionary)
        #if targetEnvironment(simulator)
        UserDefaults.standard.removeObject(forKey: simulatorPrefix + key)
        #endif
    }
}
