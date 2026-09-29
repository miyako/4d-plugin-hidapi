# 4d-plugin-hidapi

Communicate with USB and Bluetooth HID (Human Interface Device) hardware — barcode scanners, scales, foot pedals, custom USB gadgets, game controllers — directly from 4D code. The plugin wraps [signal11/hidapi](https://github.com/signal11/hidapi), which drives IOKit's `IOHIDManager`/`IOHIDDevice` on macOS and the `HidD_*`/`SetupDi*` APIs plus overlapped file I/O on Windows. You enumerate devices as a `Collection` of objects, open one to get an integer device handle, and exchange raw reports as `BLOB`s. Every command except [`hid_close`](#hid_close) returns a status `Object`.

| Command | Returns | Purpose |
|:--|:--|:--|
| [`hid_enumerate`](#hid_enumerate) | `Collection` | List every HID device attached to the system |
| [`hid_open`](#hid_open) | `Object` | Open a device by vendor ID, product ID and optional serial number |
| [`hid_open_path`](#hid_open_path) | `Object` | Open a device by its platform path (from `hid_enumerate`) |
| [`hid_close`](#hid_close) | — | Close an open device |
| [`hid_write`](#hid_write) | `Object` | Send an output report |
| [`hid_read`](#hid_read) | `Object` | Receive an input report, with optional timeout |
| [`hid_send_feature_report`](#hid_send_feature_report) | `Object` | Send a feature report |
| [`hid_get_feature_report`](#hid_get_feature_report) | `Object` | Request a feature report |
| [`hid_set_nonblocking`](#hid_set_nonblocking) | `Object` | Switch a device between blocking and non-blocking reads |

**Platforms:** macOS, Windows (32-bit and 64-bit)

---

## Requirements & platform notes

- **Which build this describes.** This reference documents the plugin built from the corrected `4DPlugin-HIDAPI.cpp` (September 2026 review). Binaries built from earlier source behave differently in ways that matter: `success` was `false` for every *successful* write/read/feature report (and `true` for a read that timed out), there was no `length` property, [`hid_read`](#hid_read) always returned a BLOB the full size you passed in, [`hid_get_feature_report`](#hid_get_feature_report) always requested report ID `0`, closing a device twice could crash 4D, and passing an empty BLOB could crash 4D on macOS. If you are running an older binary, rebuild before relying on anything below.
- **4D version.** The author tags the plugin for 4D v17. It uses `Collection` and `Object` results, so v17 or later is required.
- **Reads can block forever, and block every other plugin call while they wait.** `hid_read` with a `milliseconds` value of `0` (or omitted) uses the device's current mode, and a freshly opened device is in **blocking** mode: the call waits until the device sends something. While any `hid_read` is waiting, every other HID command from every process — including `hid_close` — waits too. Called from a cooperative process, this freezes the whole 4D application. Always either call [`hid_set_nonblocking`](#hid_set_nonblocking) with `1` right after opening, or pass a positive timeout to [`hid_read`](#hid_read). See [Error handling & troubleshooting](#error-handling--troubleshooting).
- **On macOS, opening a device takes exclusive control of it.** The underlying library opens devices with IOKit's "seize" option, so an opened keyboard, mouse or trackpad stops working for the rest of the system until you call [`hid_close`](#hid_close). The plugin's own test method carries this warning. Filter `hid_enumerate` results by `vendor_id`/`product_id` or `usage_page` before opening anything.
- **On macOS 10.15 and later**, opening keyboard-class devices may require the host application (4D) to be granted Input Monitoring permission in System Settings › Privacy & Security. This is enforced by macOS, not by the plugin; if opening a keyboard-type device fails while other devices open fine, check this first.
- **The first byte of every report BLOB is the report ID.** For devices that don't use numbered reports, that byte must be `0`. This applies to [`hid_write`](#hid_write), [`hid_send_feature_report`](#hid_send_feature_report) and [`hid_get_feature_report`](#hid_get_feature_report). An empty BLOB is rejected with an error.
- **Commands are declared thread-safe** and can be called from preemptive processes. Enumeration and opening are serialized internally; all I/O on open devices is serialized through a single lock shared by all devices.
- **Device handles are plain integers** local to the running 4D application. They don't survive a restart, and a closed handle is not reused immediately.

---

## hid_enumerate

### Syntax

```4d
devices:=hid_enumerate
```

| Parameter | Type | Description |
|:--|:--|:--|
| Result | `Collection` | One object per HID interface currently attached (see below) |

Each element of the collection has these properties:

| Property | Type | Description |
|:--|:--|:--|
| `path` | `Text` | Platform device path; pass it to [`hid_open_path`](#hid_open_path) |
| `vendor_id` | `Real` | USB vendor ID (VID) |
| `product_id` | `Real` | USB product ID (PID) |
| `serial_number` | `Text` | Serial number string; property absent if the device reports none |
| `release_number` | `Real` | Device release number (BCD, e.g. `0x0100` for 1.00) |
| `manufacturer_string` | `Text` | Manufacturer name; property absent if unavailable |
| `product_string` | `Text` | Product name; property absent if unavailable |
| `usage_page` | `Real` | Primary HID usage page (e.g. `1` = Generic Desktop) |
| `usage` | `Real` | Primary HID usage within that page (e.g. `6` = keyboard) |
| `interface_number` | `Real` | USB interface number, or `-1` if unknown |

### Description

Lists every HID device the operating system currently exposes, with no filtering. A single physical product can appear several times, once per HID interface or top-level collection — for example a keyboard with media keys often shows up as two or three entries with different `usage_page`/`usage` values and paths.

**On macOS**, `path` is an IORegistry path (`IOService:/…`), and `interface_number` is always `-1`.

**On Windows**, `path` is a device interface path (`\\?\hid#vid_…`). `interface_number` is parsed from the path's `&mi_` segment for composite USB devices and is `-1` otherwise. Text properties are omitted when Windows refuses to return the string for that device.

If the library failed to initialize when the plugin loaded, or no HID devices are attached, the result is an empty collection. No 4D error is raised.

### Example

From the plugin's own test method (`TEST.4dm`):

```4d
C_COLLECTION:C1488($devices)
C_OBJECT:C1216($device)

$devices:=hid_enumerate 

For each ($device;$devices)
	  //warning: if you open your keyboard or track pad, it will become temporarily unresponsive!
	$status:=hid_open_path ($device.path)
	If ($status.success)
		
		hid_set_nonblocking ($status.device;1)
		hid_close ($status.device)
	End if 
End for each 
```

Find the entries belonging to one product without opening anything:

```4d
C_COLLECTION($devices;$matches)
C_OBJECT($device)

$devices:=hid_enumerate
$matches:=New collection

For each ($device;$devices)
	If (($device.vendor_id=1133) & ($device.product_id=49948))  // VID 0x046D, PID 0xC31C
		$matches.push($device)
	End if 
End for each 
```

Skip keyboards and mice (Generic Desktop page `1`, usages `6` and `2`) when scanning for a custom device:

```4d
For each ($device;$devices)
	If (Not(($device.usage_page=1) & (($device.usage=6) | ($device.usage=2))))
		// safe candidate to open
	End if 
End for each 
```

---

## hid_open

### Syntax

```4d
status:=hid_open(vendor_id;product_id;serial_number)
```

| Parameter | Type | Description |
|:--|:--|:--|
| `vendor_id` | `Longint` | USB vendor ID, `0` to match any vendor |
| `product_id` | `Longint` | USB product ID, `0` to match any product |
| `serial_number` | `Text` | Serial number to match; `""` opens the first device matching the IDs |
| Result | `Object` | Status object (see below) |

Status object properties:

| Property | Type | Description |
|:--|:--|:--|
| `success` | `Boolean` | `True` if the device was opened |
| `device` | `Real` | Device handle to pass to the other commands; present only on success |
| `manufacturer_string` | `Text` | Present only if the device returned it |
| `product_string` | `Text` | Present only if the device returned it |
| `serial_number_string` | `Text` | Present only if the device returned it |
| `error` | `Text` | Present only if the library isn't initialized or an internal exception occurred |

### Description

Opens the first HID device whose vendor and product ID match (and whose serial number matches, if you pass a non-empty one). All three parameters are mandatory; pass `""` when you don't care about the serial number.

When a product exposes several HID interfaces, `hid_open` opens whichever one the library finds first, which may not be the interface you need. If you need a specific interface or usage page, pick the entry from [`hid_enumerate`](#hid_enumerate) and use [`hid_open_path`](#hid_open_path) instead.

A failed open (no matching device, device already opened exclusively by another application, permission refused) returns `success` = `False` with **no** `error` property — the underlying library doesn't report why.

**On macOS**, the device is seized exclusively until closed (see [Requirements & platform notes](#requirements--platform-notes)).

### Example

```4d
C_OBJECT($status)
C_LONGINT($device)

$status:=hid_open(1133;49948;"")  // VID 0x046D, PID 0xC31C, any serial

If ($status.success)
	$device:=$status.device
	hid_set_nonblocking($device;1)
	// ... read/write ...
	hid_close($device)
Else 
	ALERT("Device not found or could not be opened.")
End if 
```

Open one specific unit when several identical devices are plugged in:

```4d
$status:=hid_open(1133;49948;"A1B2C3D4")
```

---

## hid_open_path

### Syntax

```4d
status:=hid_open_path(path)
```

| Parameter | Type | Description |
|:--|:--|:--|
| `path` | `Text` | A `path` value taken from [`hid_enumerate`](#hid_enumerate) |
| Result | `Object` | Status object, same shape as [`hid_open`](#hid_open) |

### Description

Opens exactly the interface identified by `path`. This is the reliable way to open a specific interface of a multi-interface product. Paths are platform-specific and can change when the device is unplugged or moved to another port, so always take them from a fresh [`hid_enumerate`](#hid_enumerate) rather than storing them.

The result object has the same properties as [`hid_open`](#hid_open), and fails the same way (`success` = `False`, no `error`) if the path is stale or the device can't be opened.

### Example

From the plugin's own test method (`TEST.4dm`) — see [`hid_enumerate`](#hid_enumerate) for the full method:

```4d
	$status:=hid_open_path ($device.path)
	If ($status.success)
		
		hid_set_nonblocking ($status.device;1)
		hid_close ($status.device)
	End if 
```

Open the vendor-defined interface of a device (usage pages `65280`–`65535`, i.e. `0xFF00`–`0xFFFF`), which is where custom devices usually expose their data:

```4d
C_COLLECTION($devices)
C_OBJECT($device;$status)
C_LONGINT($handle)

$devices:=hid_enumerate
$handle:=0

For each ($device;$devices)
	If (($device.vendor_id=1133) & ($device.usage_page>=65280) & ($handle=0))
		$status:=hid_open_path($device.path)
		If ($status.success)
			$handle:=$status.device
		End if 
	End if 
End for each 
```

---

## hid_close

### Syntax

```4d
hid_close(device)
```

| Parameter | Type | Description |
|:--|:--|:--|
| `device` | `Longint` | Handle returned by [`hid_open`](#hid_open) or [`hid_open_path`](#hid_open_path) |
| Result | — | No return value |

### Description

Closes the device and invalidates the handle. Closing an unknown or already-closed handle does nothing. On macOS this also releases the exclusive seize, so a keyboard or pointing device becomes usable again.

If a [`hid_read`](#hid_read) on any device is currently waiting, `hid_close` waits for it to finish first (see [Requirements & platform notes](#requirements--platform-notes)).

Devices still open when 4D quits are closed automatically, unless a read is still blocking at that moment, in which case they are left for the operating system to reclaim.

### Example

```4d
hid_close($device)
$device:=0
```

---

## hid_write

### Syntax

```4d
status:=hid_write(device;data)
```

| Parameter | Type | Description |
|:--|:--|:--|
| `device` | `Longint` | Open device handle |
| `data` | `BLOB` | Report ID byte followed by the report data; at least 1 byte |
| Result | `Object` | `success` (`Boolean`), `length` (`Real`, bytes written) on success, `error` (`Text`) on failure |

### Description

Sends an output report. Byte `0` of `data` **must** be the report ID, or `0` if the device doesn't use numbered reports; the remaining bytes are the report payload. So an 8-byte report on an unnumbered device needs a 9-byte BLOB.

`length` is the number of bytes the library reports as written.

**On Windows**, a BLOB shorter than the device's output report size is padded with zeros to that size, and `length` reports the padded size, which can be larger than your BLOB.

**On macOS**, `length` equals the BLOB size you passed.

`error` is set to `"invalid device"` for an unknown handle and to `"data must contain at least 1 byte (the report ID)"` for an empty BLOB. For a failed transfer, **on Windows** `error` contains the system's error message; **on macOS** the library provides no message, so `success` is `False` with no `error` property.

### Example

Send a 2-byte command to an unnumbered device:

```4d
C_BLOB($data)
C_OBJECT($status)

SET BLOB SIZE($data;3;0)
$data{0}:=0    // report ID: device doesn't use numbered reports
$data{1}:=128  // command byte (0x80)
$data{2}:=1    // argument

$status:=hid_write($device;$data)

If (Not($status.success))
	ALERT("Write failed.")  // $status.error holds the reason when available (Windows)
End if 
```

---

## hid_read

### Syntax

```4d
status:=hid_read(device;data{;milliseconds})
```

| Parameter | Type | Description |
|:--|:--|:--|
| `device` | `Longint` | Open device handle |
| `data` | `BLOB` | In: its size sets the maximum number of bytes to read (at least 1). Out: the bytes actually received |
| `milliseconds` | `Longint` | Timeout. `0` or omitted: use the device's blocking/non-blocking mode. Positive: wait at most this long. `-1`: wait indefinitely |
| Result | `Object` | `success` (`Boolean`), `length` (`Real`, bytes received) on success, `error` (`Text`) on failure |

### Description

Receives one input report. Before calling, size `data` to at least the device's input report length (plus one byte if the device uses numbered reports). On return, `data` is resized to exactly the bytes received:

- `success` = `True`, `length` > `0`: a report was received and is in `data`.
- `success` = `True`, `length` = `0`: nothing arrived in time (timeout, or non-blocking mode with nothing queued). `data` is returned empty.
- `success` = `False`: the read failed, typically because the device was unplugged. `data` is left unchanged.

If the device uses numbered reports, byte `0` of the result is the report ID; otherwise the result starts directly with report data.

`milliseconds` is optional according to the plugin author's documentation; omitting it behaves like `0`. With `0`, the call is as blocking as the device mode: a freshly opened device is in blocking mode and the call **waits until data arrives, with no limit**, holding up every other plugin command meanwhile. Use [`hid_set_nonblocking`](#hid_set_nonblocking) or a positive timeout.

Negative values other than `-1` behave differently per platform: **on macOS** they are treated as "don't wait"; **on Windows** they are treated as an extremely long wait (about 49 days), which is effectively a hang. Only pass `0`, `-1` or a positive value.

`error` is set to `"invalid device"` for an unknown handle and to `"data must contain at least 1 byte (the report ID)"` if `data` is empty. For a failed transfer, `error` is set **on Windows** only.

### Example

Poll with a short timeout:

```4d
C_BLOB($data)
C_OBJECT($status)

SET BLOB SIZE($data;65;0)  // 64-byte report + report ID
$status:=hid_read($device;$data;250)  // wait at most 250 ms

Case of 
	: (Not($status.success))
		ALERT("Read failed; device may have been disconnected.")
	: ($status.length=0)
		// nothing received this time
	Else 
		// BLOB size($data) = $status.length
		// process $data{0} .. $data{$status.length-1}
End case 
```

Drain everything queued, in non-blocking mode:

```4d
C_BOOLEAN($more)

hid_set_nonblocking($device;1)
$more:=True

While ($more)
	SET BLOB SIZE($data;65;0)
	$status:=hid_read($device;$data)
	$more:=($status.success & ($status.length>0))
	If ($more)
		// process $data
	End if 
End while 
```

---

## hid_send_feature_report

### Syntax

```4d
status:=hid_send_feature_report(device;data)
```

| Parameter | Type | Description |
|:--|:--|:--|
| `device` | `Longint` | Open device handle |
| `data` | `BLOB` | Report ID byte followed by the report data; at least 1 byte |
| Result | `Object` | `success` (`Boolean`), `length` (`Real`, bytes sent) on success, `error` (`Text`) on failure |

### Description

Sends a feature report over the control endpoint. Feature reports are typically used for device configuration rather than data streaming. As with [`hid_write`](#hid_write), byte `0` **must** be the report ID (`0` for unnumbered devices), so a 16-byte feature report needs a 17-byte BLOB, and `length` counts that ID byte.

Error reporting follows [`hid_write`](#hid_write): `"invalid device"`, the empty-BLOB message, a system message **on Windows**, and no message **on macOS**.

### Example

```4d
C_BLOB($data)
C_OBJECT($status)

SET BLOB SIZE($data;17;0)
$data{0}:=2   // report ID 2
$data{1}:=1   // e.g. "enable" flag in the device's own protocol

$status:=hid_send_feature_report($device;$data)
```

---

## hid_get_feature_report

### Syntax

```4d
status:=hid_get_feature_report(device;data)
```

| Parameter | Type | Description |
|:--|:--|:--|
| `device` | `Longint` | Open device handle |
| `data` | `BLOB` | In: byte `0` = report ID to request (`0` for unnumbered devices); size = maximum bytes to receive, including the ID byte. Out: the report received |
| Result | `Object` | `success` (`Boolean`), `length` (`Real`, bytes received including the report ID byte) on success, `error` (`Text`) on failure |

### Description

Requests a feature report from the device. Set byte `0` of `data` to the report ID you want and size the BLOB to the report length plus one. On success, `data` is resized to `length` bytes: byte `0` still holds the report ID and the report data starts at byte `1`. On failure, `data` is left unchanged.

Error reporting follows [`hid_write`](#hid_write).

### Example

```4d
C_BLOB($data)
C_OBJECT($status)

SET BLOB SIZE($data;17;0)
$data{0}:=2   // request report ID 2

$status:=hid_get_feature_report($device;$data)

If ($status.success)
	// $data{0} = 2, report data in $data{1} .. $data{$status.length-1}
End if 
```

---

## hid_set_nonblocking

### Syntax

```4d
status:=hid_set_nonblocking(device;nonblocking)
```

| Parameter | Type | Description |
|:--|:--|:--|
| `device` | `Longint` | Open device handle |
| `nonblocking` | `Longint` | `1` = non-blocking, `0` = blocking (the default for a newly opened device) |
| Result | `Object` | `success` (`Boolean`); `error` (`Text`) on failure |

### Description

Sets how [`hid_read`](#hid_read) behaves when called with `milliseconds` = `0` (or omitted). In non-blocking mode, `hid_read` returns immediately with `length` = `0` when no report is queued. In blocking mode it waits until one arrives. A positive or `-1` timeout passed to [`hid_read`](#hid_read) overrides this mode for that call.

You can switch modes at any time. The setting is per device and lasts until the device is closed. It only changes a flag inside the library, so it succeeds for any valid handle; `error` is `"invalid device"` for an unknown one.

### Example

From the plugin's own test method (`TEST.4dm`):

```4d
		hid_set_nonblocking ($status.device;1)
```

---

## Error handling & troubleshooting

- **No 4D errors are raised.** Every failure is reported through the result object (`success` = `False`, sometimes with `error`) or, for [`hid_enumerate`](#hid_enumerate), as an empty collection. Always test `success`.
- **4D freezes on `hid_read`.** The device is in blocking mode and sent nothing. Call [`hid_set_nonblocking`](#hid_set_nonblocking) with `1` after opening, or always pass a positive timeout. Run long polling loops in a worker or preemptive process rather than the main process, and keep individual timeouts short, because a waiting read holds up every other HID command in the application.
- **A negative timeout hangs on Windows.** Only `-1` means "wait forever" on both platforms; any other negative value is about 49 days on Windows and "don't wait" on macOS. Use `0`, `-1` or a positive value.
- **Keyboard or trackpad stops responding on macOS.** You opened it, and the library seizes opened devices exclusively. Close it with [`hid_close`](#hid_close), and filter by IDs or `usage_page` before opening.
- **`hid_open` returns `success` = `False` with no `error`.** No matching device, the device is held exclusively by another application, or (**on macOS 10.15+**, for keyboard-class devices) Input Monitoring permission hasn't been granted. The library gives no reason. Check the device is present in [`hid_enumerate`](#hid_enumerate) first.
- **`hid_open` opens the wrong interface.** Multi-interface products appear several times in [`hid_enumerate`](#hid_enumerate). Pick the entry by `usage_page`/`usage`/`interface_number` and use [`hid_open_path`](#hid_open_path).
- **Transfers fail with no `error` on macOS.** The macOS library never supplies error messages. **On Windows**, `error` carries the system message.
- **`"data must contain at least 1 byte (the report ID)"`.** You passed an empty BLOB. Report BLOBs always start with the report ID byte (`0` for unnumbered devices).
- **The device ignores writes or returns garbage.** The report ID byte is usually missing or wrong. For unnumbered devices, byte `0` must be `0` and the payload starts at byte `1`. On Windows, also check that the BLOB is at least the device's output report size (shorter ones are zero-padded).
- **`hid_read` reports `success` with `length` = `0`.** Not an error: nothing arrived within the timeout, or nothing was queued in non-blocking mode.
- **`"invalid device"`.** The handle was never opened, was already closed, or belongs to a previous session of the 4D application.
- **`"hidapi is not initialized"`.** The library failed to start when the plugin loaded. Restart 4D; if it persists, the plugin can't reach the system's HID services on this machine.
- **The device was unplugged.** Reads and writes start returning `success` = `False`. Call [`hid_close`](#hid_close), then re-enumerate and reopen when it's reconnected — paths and handles are not stable across reconnections.

---

## Quick reference

```4d
C_COLLECTION($devices)
C_OBJECT($d;$status)
C_LONGINT($h)
C_BLOB($data)

$devices:=hid_enumerate
$h:=0
For each ($d;$devices)
	If (($d.vendor_id=1133) & ($d.product_id=49948) & ($h=0))
		$status:=hid_open_path($d.path)
		If ($status.success)
			$h:=$status.device
		End if 
	End if 
End for each 

If ($h#0)
	hid_set_nonblocking($h;1)
	
	SET BLOB SIZE($data;65;0)
	$data{0}:=0
	$data{1}:=128
	$status:=hid_write($h;$data)
	
	SET BLOB SIZE($data;65;0)
	$status:=hid_read($h;$data;250)
	If ($status.success & ($status.length>0))
		// use $data
	End if 
	
	SET BLOB SIZE($data;17;0)
	$data{0}:=2
	$status:=hid_get_feature_report($h;$data)
	
	hid_close($h)
End if 
```
