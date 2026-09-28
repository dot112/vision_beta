import asyncio
from asyncua import Client, ua


OPC_URL = "opc.tcp://192.168.0.1:4840"
TAG_NAME = "Tag_1" # Change this to your symbolic tag name


async def find_node(node, target_name, depth=0, max_depth=8, visited=None):
    if visited is None:
        visited = set()

    if depth > max_depth:
        return None

    try:
        node_key = str(node.nodeid)

        if node_key in visited:
            return None

        visited.add(node_key)
    except Exception:
        pass

    # Check this node
    try:
        browse_name = await node.read_browse_name()

        if browse_name.Name == target_name:
            return node

    except Exception:
        pass

    # Search children
    try:
        children = await node.get_children()
    except Exception:
        return None

    for child in children:
        result = await find_node(
            child,
            target_name,
            depth + 1,
            max_depth,
            visited
        )

        if result is not None:
            return result

    return None


async def toggle_m0():
    client = Client(
        OPC_URL,
        timeout=5
    )

    try:
        print(f"Connecting to {OPC_URL}...")
        await client.connect()
        print("Connected.")

        # Find symbolic M0.0 tag
        print(f"Searching for {TAG_NAME}...")

        node = await find_node(
            client.nodes.root,
            TAG_NAME
        )

        if node is None:
            print(
                f"ERROR: {TAG_NAME} was not found."
            )
            print(
                "Expose the PLC tag through OPC UA in TIA Portal."
            )
            return

        print("FOUND")
        print("NodeId:", node.nodeid)

        # Check access
        access = await node.read_attribute(
            ua.AttributeIds.AccessLevel
        )

        if access.Value is not None:
            access_level = access.Value.Value
            print("AccessLevel:", access_level)

            # Bit 1 = CurrentWrite
            if not (access_level & 0x02):
                print("ERROR: Tag is not writable.")
                return

        # Read current state
        current = await node.read_value()

        print("Current M0.0:", current)

        # Toggle
        new_value = not bool(current)

        print(
            f"Writing M0.0 = {new_value}"
        )

        # IMPORTANT:
        # Send a value-only DataValue.
        data_value = ua.DataValue(
            ua.Variant(
                new_value,
                ua.VariantType.Boolean
            )
        )

        await node.write_value(
            data_value
        )

        # Read back
        actual = await node.read_value()

        print(
            f"M0.0 after write: {actual}"
        )

        if bool(actual) == new_value:
            print("TOGGLE SUCCESS")
        else:
            print("WRITE DID NOT READ BACK AS EXPECTED")

    except Exception as e:
        print(
            f"ERROR: {type(e).__name__}: {e}"
        )

    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(toggle_m0())