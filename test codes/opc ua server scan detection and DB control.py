import asyncio
import ipaddress
import socket
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext

from asyncua import Client, ua


# ============================================================
# CONFIGURATION
# ============================================================

NETWORK = "192.168.0.0/24"
OPC_UA_PORT = 4840

DEFAULT_PLC_IP = "192.168.0.1"

# Your Siemens DB
DB_NAME = "opc data"

# DB members
BOOL_NAME = "data_bool"
INT_NAME = "data_int"
STRING_NAME = "data_string"

# Network scan timeout
SCAN_TIMEOUT = 0.35

# OPC UA connection timeout
OPC_TIMEOUT = 5


# ============================================================
# GLOBAL STATE
# ============================================================

selected_plc_ip = None

# Store NodeId objects discovered from the server.
node_ids = {
    "db": None,
    "bool": None,
    "int": None,
    "string": None,
}

scan_running = False


# ============================================================
# LOGGING
# ============================================================

def log(message):
    try:
        root.after(
            0,
            lambda: (
                log_box.insert(tk.END, message + "\n"),
                log_box.see(tk.END)
            )
        )
    except Exception:
        pass


def set_status(text, color="#9ca3af"):
    try:
        root.after(
            0,
            lambda: status_label.config(
                text=text,
                fg=color
            )
        )
    except Exception:
        pass


# ============================================================
# TCP SCAN
# ============================================================

def check_port(ip, port):
    try:
        with socket.create_connection(
            (ip, port),
            timeout=SCAN_TIMEOUT
        ):
            return True

    except Exception:
        return False


def scan_network():

    global scan_running

    if scan_running:
        return

    scan_running = True

    log("")
    log("=" * 70)
    log(
        f"Scanning {NETWORK} for OPC UA servers..."
    )
    log("=" * 70)

    set_status(
        "Scanning network...",
        "#fbbf24"
    )

    def worker():

        try:

            network = ipaddress.ip_network(
                NETWORK,
                strict=False
            )

            hosts = [
                str(ip)
                for ip in network.hosts()
            ]

            from concurrent.futures import ThreadPoolExecutor

            found = []

            with ThreadPoolExecutor(
                max_workers=64
            ) as executor:

                futures = {
                    executor.submit(
                        check_port,
                        ip,
                        OPC_UA_PORT
                    ): ip
                    for ip in hosts
                }

                for future in futures:

                    ip = futures[future]

                    try:

                        if future.result():

                            found.append(ip)

                            log(
                                f"[OPEN] "
                                f"{ip}:{OPC_UA_PORT}"
                            )

                    except Exception:
                        pass

            root.after(
                0,
                lambda: scan_complete(found)
            )

        except Exception as e:

            root.after(
                0,
                lambda: scan_failed(str(e))
            )

    threading.Thread(
        target=worker,
        daemon=True
    ).start()


def scan_complete(found):

    global scan_running

    scan_running = False

    if not found:

        set_status(
            "No OPC UA candidates found",
            "#ef4444"
        )

        log(
            "No OPC UA candidates found."
        )

        return

    log("")
    log(
        f"Found {len(found)} candidate(s)."
    )

    set_status(
        "Identifying OPC UA servers...",
        "#fbbf24"
    )

    threading.Thread(
        target=identify_servers,
        args=(found,),
        daemon=True
    ).start()


def scan_failed(error):

    global scan_running

    scan_running = False

    set_status(
        "Scan failed",
        "#ef4444"
    )

    log(
        f"SCAN ERROR: {error}"
    )


# ============================================================
# OPC UA SERVER IDENTIFICATION
# ============================================================

async def inspect_server(ip):

    url = (
        f"opc.tcp://{ip}:{OPC_UA_PORT}"
    )

    client = Client(
        url,
        timeout=OPC_TIMEOUT
    )

    try:

        endpoints = (
            await client.connect_and_get_server_endpoints()
        )

        is_siemens = False

        for endpoint in endpoints:

            try:

                app_uri = str(
                    endpoint.Server.ApplicationUri
                )

            except Exception:

                app_uri = ""

            try:

                app_name = (
                    endpoint.Server.ApplicationName.Text
                )

            except Exception:

                app_name = ""

            if (
                "SIMATIC.S7-1500.OPC-UA.Application"
                in app_uri
                or
                "SIMATIC"
                in app_name
            ):

                is_siemens = True

        return {
            "ip": ip,
            "url": url,
            "endpoints": endpoints,
            "is_siemens": is_siemens
        }

    except Exception as e:

        log(
            f"[NOT OPC UA] {ip}: {e}"
        )

        return None

    finally:

        try:
            await client.disconnect()
        except Exception:
            pass


def identify_servers(ips):

    async def run():

        siemens_servers = []

        for ip in ips:

            result = await inspect_server(ip)

            if not result:
                continue

            log("")
            log(
                f"OPC UA SERVER: {result['url']}"
            )

            for endpoint in result["endpoints"]:

                try:
                    name = (
                        endpoint.Server.ApplicationName.Text
                    )
                except Exception:
                    name = "Unknown"

                log(
                    f"  Name: {name}"
                )

                log(
                    f"  Security: "
                    f"{endpoint.SecurityPolicyUri}"
                )

                log(
                    f"  Mode: "
                    f"{endpoint.SecurityMode}"
                )

            if result["is_siemens"]:

                siemens_servers.append(
                    result
                )

        root.after(
            0,
            lambda: identification_complete(
                siemens_servers
            )
        )

    asyncio.run(run())


def identification_complete(servers):

    global selected_plc_ip

    if not servers:

        set_status(
            "Siemens PLC not found",
            "#ef4444"
        )

        log("")
        log(
            "No Siemens S7 OPC UA server found."
        )

        return

    selected = None

    # Prefer your known PLC
    for server in servers:

        if server["ip"] == DEFAULT_PLC_IP:

            selected = server
            break

    if selected is None:

        selected = servers[0]

    selected_plc_ip = selected["ip"]

    root.after(
        0,
        lambda: (
            ip_entry.delete(
                0,
                tk.END
            ),
            ip_entry.insert(
                0,
                selected_plc_ip
            )
        )
    )

    log("")
    log("=" * 70)
    log("SIEMENS PLC FOUND")
    log("=" * 70)

    log(
        f"IP: {selected_plc_ip}"
    )

    log(
        f"URL: "
        f"opc.tcp://"
        f"{selected_plc_ip}:"
        f"{OPC_UA_PORT}"
    )

    set_status(
        "Siemens PLC found",
        "#22c55e"
    )


# ============================================================
# OPC UA BROWSE
# ============================================================

async def find_node_recursive(
    start_node,
    target_name,
    max_depth=10,
    depth=0,
    visited=None
):

    if visited is None:
        visited = set()

    if depth > max_depth:
        return None

    try:

        current_id = str(
            start_node.nodeid
        )

        if current_id in visited:

            return None

        visited.add(current_id)

    except Exception:
        pass

    # --------------------------------------------------------
    # Check current node
    # --------------------------------------------------------

    try:

        browse_name = (
            await start_node.read_browse_name()
        )

        if browse_name.Name == target_name:

            return start_node

    except Exception:
        pass

    # --------------------------------------------------------
    # Browse children
    # --------------------------------------------------------

    try:

        children = (
            await start_node.get_children()
        )

    except Exception:

        return None

    for child in children:

        result = await find_node_recursive(
            child,
            target_name,
            max_depth=max_depth,
            depth=depth + 1,
            visited=visited
        )

        if result:

            return result

    return None


# ============================================================
# DISCOVER DB
# ============================================================

async def discover_database(ip):

    global node_ids

    url = (
        f"opc.tcp://"
        f"{ip}:"
        f"{OPC_UA_PORT}"
    )

    client = Client(
        url,
        timeout=OPC_TIMEOUT
    )

    try:

        log("")
        log("=" * 70)
        log("CONNECTING TO OPC UA SERVER")
        log("=" * 70)

        await client.connect()

        log(
            "OPC UA connection successful."
        )

        set_status(
            "Connected",
            "#22c55e"
        )

        root_node = client.nodes.root

        # ----------------------------------------------------
        # DATABASE
        # ----------------------------------------------------

        log("")
        log(
            f"Searching for DB: {DB_NAME}"
        )

        db_node = await find_node_recursive(
            root_node,
            DB_NAME,
            max_depth=10
        )

        if not db_node:

            log(
                f"DB NOT FOUND: {DB_NAME}"
            )

            set_status(
                "DB not found",
                "#fbbf24"
            )

            return

        # IMPORTANT:
        # Store the real NodeId object.
        node_ids["db"] = db_node.nodeid

        log("")
        log("DB FOUND")

        log(
            f"DB NodeId: "
            f"{db_node.nodeid}"
        )

        try:

            log(
                f"DB NodeId string: "
                f"{db_node.nodeid.to_string()}"
            )

        except Exception:
            pass

        # ----------------------------------------------------
        # DB MEMBERS
        # ----------------------------------------------------

        await discover_member(
            db_node,
            BOOL_NAME,
            "bool"
        )

        await discover_member(
            db_node,
            INT_NAME,
            "int"
        )

        await discover_member(
            db_node,
            STRING_NAME,
            "string"
        )

        found = sum(
            node_ids[key] is not None
            for key in (
                "bool",
                "int",
                "string"
            )
        )

        log("")
        log("=" * 70)
        log(
            f"DISCOVERY COMPLETE: "
            f"{found}/3"
        )
        log("=" * 70)

        if found == 3:

            set_status(
                "All DB variables found",
                "#22c55e"
            )

        else:

            set_status(
                f"{found}/3 variables found",
                "#fbbf24"
            )

    except Exception as e:

        set_status(
            "OPC UA error",
            "#ef4444"
        )

        log("")
        log(
            f"DISCOVERY ERROR: "
            f"{type(e).__name__}: {e}"
        )

    finally:

        try:
            await client.disconnect()
        except Exception:
            pass


async def discover_member(
    db_node,
    variable_name,
    key
):

    node = await find_node_recursive(
        db_node,
        variable_name,
        max_depth=8
    )

    if not node:

        log(
            f"NOT FOUND: {variable_name}"
        )

        return

    # IMPORTANT:
    # Keep NodeId object.
    node_ids[key] = node.nodeid

    try:

        browse_name = (
            await node.read_browse_name()
        )

        value = (
            await node.read_value()
        )

        access = (
            await node.read_attribute(
                ua.AttributeIds.AccessLevel
            )
        )

        log("")
        log(
            f"FOUND: {variable_name}"
        )

        log(
            f"  NodeId: {node.nodeid}"
        )

        try:

            log(
                f"  NodeId string: "
                f"{node.nodeid.to_string()}"
            )

        except Exception:
            pass

        log(
            f"  BrowseName: "
            f"{browse_name.Name}"
        )

        log(
            f"  Value: {value}"
        )

        if access.Value is not None:

            access_level = (
                access.Value.Value
            )

            log(
                f"  AccessLevel: "
                f"{access_level}"
            )

            if access_level & 0x02:

                log(
                    "  Writable: YES"
                )

            else:

                log(
                    "  Writable: NO"
                )

    except Exception as e:

        log(
            f"Metadata error for "
            f"{variable_name}: {e}"
        )


# ============================================================
# CONNECT / DISCOVER BUTTON
# ============================================================

def connect_clicked():

    global selected_plc_ip

    ip = ip_entry.get().strip()

    if not ip:

        messagebox.showerror(
            "PLC IP",
            "Enter the PLC IP address."
        )

        return

    selected_plc_ip = ip

    # Clear previous NodeIds
    node_ids["db"] = None
    node_ids["bool"] = None
    node_ids["int"] = None
    node_ids["string"] = None

    threading.Thread(
        target=lambda: asyncio.run(
            discover_database(ip)
        ),
        daemon=True
    ).start()


# ============================================================
# WRITE FUNCTION
# ============================================================

async def write_node_value(
    key,
    value,
    variant_type
):

    if not selected_plc_ip:

        raise RuntimeError(
            "No PLC selected."
        )

    node_id = node_ids.get(key)

    if node_id is None:

        raise RuntimeError(
            f"{key} NodeId not found. "
            f"Run CONNECT / DISCOVER first."
        )

    url = (
        f"opc.tcp://"
        f"{selected_plc_ip}:"
        f"{OPC_UA_PORT}"
    )

    log("")
    log(
        f"Connecting for WRITE:"
    )

    log(
        url
    )

    client = Client(
        url,
        timeout=OPC_TIMEOUT
    )

    try:

        await client.connect()

        log(
            "Write connection established."
        )

        # Recreate Node in the new client session.
        node = client.get_node(
            node_id
        )

        log(
            f"NodeId: {node_id}"
        )

        # ----------------------------------------------------
        # Check AccessLevel
        # ----------------------------------------------------

        try:

            access = await node.read_attribute(
                ua.AttributeIds.AccessLevel
            )

            if access.Value is not None:

                access_level = (
                    access.Value.Value
                )

                log(
                    f"AccessLevel: "
                    f"{access_level}"
                )

                if not (access_level & 0x02):

                    raise RuntimeError(
                        "Server reports this node "
                        "is not writable."
                    )

        except RuntimeError:
            raise

        except Exception as e:

            log(
                f"AccessLevel read failed: {e}"
            )

        # ----------------------------------------------------
        # CRITICAL FIX
        #
        # Use DataValue containing ONLY the Variant.
        #
        # Passing Variant directly to write_value()
        # causes current asyncua versions to add a
        # SourceTimestamp automatically.
        #
        # Siemens S7-1500 OPC UA can reject that with:
        # BadWriteNotSupported
        # ----------------------------------------------------

        data_value = ua.DataValue(
            ua.Variant(
                value,
                variant_type
            )
        )

        await node.write_value(
            data_value
        )

        log(
            f"WRITE SUCCESS: "
            f"{value}"
        )

        # ----------------------------------------------------
        # READ BACK
        # ----------------------------------------------------

        actual = await node.read_value()

        log(
            f"READ BACK: "
            f"{actual}"
        )

        return actual

    finally:

        try:
            await client.disconnect()
        except Exception:
            pass


# ============================================================
# BOOL
# ============================================================

def write_bool(value):

    def worker():

        try:

            actual = asyncio.run(
                write_node_value(
                    "bool",
                    bool(value),
                    ua.VariantType.Boolean
                )
            )

            root.after(
                0,
                lambda: bool_value_label.config(
                    text=str(actual),
                    fg=(
                        "#22c55e"
                        if actual
                        else "#ef4444"
                    )
                )
            )

        except Exception as e:

            log(
                f"BOOL WRITE ERROR: {e}"
            )

            root.after(
                0,
                lambda: messagebox.showerror(
                    "BOOL Write Error",
                    str(e)
                )
            )

    threading.Thread(
        target=worker,
        daemon=True
    ).start()


# ============================================================
# INT
# ============================================================

def write_integer():

    text = int_entry.get().strip()

    try:

        value = int(text)

    except ValueError:

        messagebox.showerror(
            "Invalid Integer",
            "Enter a valid integer."
        )

        return

    def worker():

        try:

            actual = asyncio.run(
                write_node_value(
                    "int",
                    value,
                    ua.VariantType.Int16
                )
            )

            root.after(
                0,
                lambda: int_value_label.config(
                    text=str(actual)
                )
            )

        except Exception as e:

            log(
                f"INT WRITE ERROR: {e}"
            )

            root.after(
                0,
                lambda: messagebox.showerror(
                    "INT Write Error",
                    str(e)
                )
            )

    threading.Thread(
        target=worker,
        daemon=True
    ).start()


# ============================================================
# STRING
# ============================================================

def write_string():

    value = string_entry.get()

    def worker():

        try:

            actual = asyncio.run(
                write_node_value(
                    "string",
                    value,
                    ua.VariantType.String
                )
            )

            root.after(
                0,
                lambda: string_value_label.config(
                    text=str(actual)
                )
            )

        except Exception as e:

            log(
                f"STRING WRITE ERROR: {e}"
            )

            root.after(
                0,
                lambda: messagebox.showerror(
                    "STRING Write Error",
                    str(e)
                )
            )

    threading.Thread(
        target=worker,
        daemon=True
    ).start()


# ============================================================
# UI
# ============================================================

root = tk.Tk()

root.title(
    "Siemens S7-1511 OPC UA DB Controller"
)

root.geometry(
    "900x780"
)

root.configure(
    bg="#1f2430"
)

root.resizable(
    False,
    False
)


# ============================================================
# TITLE
# ============================================================

tk.Label(
    root,
    text="S7-1511-1 PN",
    font=("Arial", 23, "bold"),
    fg="white",
    bg="#1f2430"
).pack(
    pady=(18, 2)
)


tk.Label(
    root,
    text="OPC UA DB Read / Write Test",
    font=("Arial", 11),
    fg="#9ca3af",
    bg="#1f2430"
).pack(
    pady=(0, 12)
)


# ============================================================
# CONNECTION CONTROLS
# ============================================================

connection_frame = tk.Frame(
    root,
    bg="#1f2430"
)

connection_frame.pack(
    pady=5
)


tk.Label(
    connection_frame,
    text="PLC IP:",
    font=("Arial", 11, "bold"),
    fg="white",
    bg="#1f2430"
).pack(
    side=tk.LEFT,
    padx=5
)


ip_entry = tk.Entry(
    connection_frame,
    width=20,
    font=("Arial", 11)
)

ip_entry.insert(
    0,
    DEFAULT_PLC_IP
)

ip_entry.pack(
    side=tk.LEFT,
    padx=5
)


tk.Button(
    connection_frame,
    text="SCAN OPC UA",
    width=14,
    height=2,
    command=scan_network
).pack(
    side=tk.LEFT,
    padx=5
)


tk.Button(
    connection_frame,
    text="CONNECT / DISCOVER",
    width=20,
    height=2,
    command=connect_clicked
).pack(
    side=tk.LEFT,
    padx=5
)


# ============================================================
# STATUS
# ============================================================

status_label = tk.Label(
    root,
    text="Ready",
    font=("Arial", 12, "bold"),
    fg="#9ca3af",
    bg="#1f2430"
)

status_label.pack(
    pady=8
)


# ============================================================
# BOOL
# ============================================================

bool_frame = tk.Frame(
    root,
    bg="#2b3140",
    padx=15,
    pady=13
)

bool_frame.pack(
    fill="x",
    padx=35,
    pady=7
)


tk.Label(
    bool_frame,
    text=f"{BOOL_NAME}   [BOOL]",
    font=("Arial", 11, "bold"),
    fg="white",
    bg="#2b3140"
).grid(
    row=0,
    column=0,
    padx=10
)


bool_value_label = tk.Label(
    bool_frame,
    text="---",
    font=("Arial", 11, "bold"),
    fg="#fbbf24",
    bg="#2b3140"
)

bool_value_label.grid(
    row=0,
    column=1,
    padx=20
)


tk.Button(
    bool_frame,
    text="TRUE",
    width=12,
    command=lambda: write_bool(True)
).grid(
    row=0,
    column=2,
    padx=5
)


tk.Button(
    bool_frame,
    text="FALSE",
    width=12,
    command=lambda: write_bool(False)
).grid(
    row=0,
    column=3,
    padx=5
)


# ============================================================
# INT
# ============================================================

int_frame = tk.Frame(
    root,
    bg="#2b3140",
    padx=15,
    pady=13
)

int_frame.pack(
    fill="x",
    padx=35,
    pady=7
)


tk.Label(
    int_frame,
    text=f"{INT_NAME}   [INT]",
    font=("Arial", 11, "bold"),
    fg="white",
    bg="#2b3140"
).grid(
    row=0,
    column=0,
    padx=10
)


int_value_label = tk.Label(
    int_frame,
    text="---",
    font=("Arial", 11, "bold"),
    fg="#fbbf24",
    bg="#2b3140"
)

int_value_label.grid(
    row=0,
    column=1,
    padx=20
)


int_entry = tk.Entry(
    int_frame,
    width=18,
    font=("Arial", 11)
)

int_entry.insert(
    0,
    "123"
)

int_entry.grid(
    row=0,
    column=2,
    padx=5
)


tk.Button(
    int_frame,
    text="WRITE",
    width=12,
    command=write_integer
).grid(
    row=0,
    column=3,
    padx=5
)


# ============================================================
# STRING
# ============================================================

string_frame = tk.Frame(
    root,
    bg="#2b3140",
    padx=15,
    pady=13
)

string_frame.pack(
    fill="x",
    padx=35,
    pady=7
)


tk.Label(
    string_frame,
    text=f"{STRING_NAME}   [STRING]",
    font=("Arial", 11, "bold"),
    fg="white",
    bg="#2b3140"
).grid(
    row=0,
    column=0,
    padx=10
)


string_value_label = tk.Label(
    string_frame,
    text="---",
    font=("Arial", 11, "bold"),
    fg="#fbbf24",
    bg="#2b3140"
)

string_value_label.grid(
    row=0,
    column=1,
    padx=20
)


string_entry = tk.Entry(
    string_frame,
    width=32,
    font=("Arial", 11)
)

string_entry.insert(
    0,
    "Hello PLC"
)

string_entry.grid(
    row=0,
    column=2,
    padx=5
)


tk.Button(
    string_frame,
    text="WRITE",
    width=12,
    command=write_string
).grid(
    row=0,
    column=3,
    padx=5
)


# ============================================================
# LOG
# ============================================================

tk.Label(
    root,
    text="OPC UA Communication Log",
    font=("Arial", 11, "bold"),
    fg="white",
    bg="#1f2430"
).pack(
    pady=(15, 5)
)


log_box = scrolledtext.ScrolledText(
    root,
    width=105,
    height=25,
    bg="#111827",
    fg="#d1d5db",
    insertbackground="white",
    font=("Consolas", 9)
)

log_box.pack(
    padx=25,
    pady=5
)


# ============================================================
# INITIAL LOG
# ============================================================

log(
    f"Endpoint: "
    f"opc.tcp://{DEFAULT_PLC_IP}:{OPC_UA_PORT}"
)

log(
    f"Database: {DB_NAME}"
)

log(
    f"Variables: "
    f"{BOOL_NAME}, "
    f"{INT_NAME}, "
    f"{STRING_NAME}"
)

log("")
log(
    "Press CONNECT / DISCOVER."
)


# ============================================================
# RUN
# ============================================================

root.mainloop()