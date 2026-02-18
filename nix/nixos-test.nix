{self, ...}: {
  imports = [
  ];
  perSystem = {
    pkgs,
    lib,
    ...
  }: let
    # Mock package for testing — just sleeps so systemd sees it as running
    mockMctomqtt = pkgs.writeShellApplication {
      name = "mctomqtt";
      text = ''
        echo "Mock mctomqtt service started with args: $*"
        while true; do sleep 10; done
      '';
    };
  in {
    checks.mctomqtt-test = pkgs.testers.runNixOSTest {
      name = "mctomqtt-test";

      nodes.machine = {
        config,
        pkgs,
        ...
      }: {
        imports = [self.nixosModules.default];

        services.mctomqtt = {
          enable = true;
          package = mockMctomqtt;
          iata = "TEST";
          serialPorts = ["/dev/ttyS1"];
          defaults.letsmesh-us.enable = false;
          defaults.letsmesh-eu.enable = true;

          brokers = [
            {
              enabled = true;
              server = "mqtt1.example.com";
              port = 1883;
              transport = "tcp";
              use-tls = false;
              tls-verify = true;
              client-id-prefix = "test_";
              qos = 1;
              retain = false;
              keepalive = 30;
              username = "user1";
              password = "pass1";
            }
          ];

          settings = {
            serial-baud-rate = 9600;
            serial-timeout = 5;
            log-level = "DEBUG";
          };
        };
      };

      testScript = ''
        import tomllib

        start_all()

        # Wait for the service to start
        machine.wait_for_unit("mctomqtt.service")
        machine.succeed("systemctl is-active --quiet mctomqtt.service")

        # Verify the user and group were created
        machine.succeed("getent passwd mctomqtt")
        machine.succeed("getent group mctomqtt")
        machine.succeed("groups mctomqtt | grep -q dialout")

        # Extract config file path from the systemd unit
        unit_content = machine.succeed("systemctl cat mctomqtt.service")
        config_path = None
        for line in unit_content.splitlines():
            if "--config" in line:
                config_path = line.split("--config")[1].strip().split()[0].rstrip(";")
                break
        assert config_path is not None, "Could not find --config in ExecStart"

        # Read and parse the generated TOML config
        config_toml = machine.succeed(f"cat {config_path}")
        config = tomllib.loads(config_toml)

        with subtest("General section"):
            assert config["general"]["iata"] == "TEST", f"iata: {config['general']['iata']}"
            assert config["general"]["log_level"] == "DEBUG", f"log_level: {config['general']['log_level']}"
            assert config["general"]["sync_time"] is True, f"sync_time: {config['general']['sync_time']}"

        with subtest("Serial section"):
            assert "/dev/ttyS1" in config["serial"]["ports"], f"ports: {config['serial']['ports']}"
            assert config["serial"]["baud_rate"] == 9600, f"baud_rate: {config['serial']['baud_rate']}"
            assert config["serial"]["timeout"] == 5, f"timeout: {config['serial']['timeout']}"

        with subtest("Topics section (hardcoded defaults)"):
            assert "meshcore/" in config["topics"]["status"]
            assert "meshcore/" in config["topics"]["packets"]
            assert "meshcore/" in config["topics"]["debug"]
            assert "meshcore/" in config["topics"]["raw"]

        with subtest("Remote serial section"):
            assert config["remote_serial"]["enabled"] is False
            assert config["remote_serial"]["allowed_companions"] == []

        with subtest("Update section"):
            assert config["update"]["repo"] == "Cisien/meshcoretomqtt"
            assert config["update"]["branch"] == "main"

        with subtest("Broker count — US disabled, EU + custom"):
            brokers = config["broker"]
            assert len(brokers) == 2, f"Expected 2 brokers, got {len(brokers)}"
            names = [b["name"] for b in brokers]
            assert "letsmesh-us" not in names, "US broker should be disabled"

        with subtest("LetsMesh EU broker"):
            eu = config["broker"][0]
            assert eu["name"] == "letsmesh-eu"
            assert eu["server"] == "mqtt-eu-v1.letsmesh.net"
            assert eu["port"] == 443
            assert eu["transport"] == "websockets"
            assert eu["keepalive"] == 60
            assert eu["qos"] == 0
            assert eu["retain"] is True
            assert eu["tls"]["enabled"] is True
            assert eu["tls"]["verify"] is True
            assert eu["auth"]["method"] == "token"
            assert eu["auth"]["audience"] == "mqtt-eu-v1.letsmesh.net"

        with subtest("Custom broker"):
            custom = config["broker"][1]
            assert custom["server"] == "mqtt1.example.com"
            assert custom["port"] == 1883
            assert custom["transport"] == "tcp"
            assert custom["keepalive"] == 30
            assert custom["qos"] == 1
            assert custom["retain"] is False
            assert custom["client_id_prefix"] == "test_"
            assert custom["tls"]["enabled"] is False
            assert custom["tls"]["verify"] is True
            assert custom["auth"]["method"] == "password"
            assert custom["auth"]["username"] == "user1"
            assert custom["auth"]["password"] == "pass1"

        with subtest("Service dependencies on serial device"):
            machine.succeed("systemctl show mctomqtt.service | grep 'After='    | grep -q 'dev-ttyS1.device'")
            machine.succeed("systemctl show mctomqtt.service | grep 'Requires=' | grep -q 'dev-ttyS1.device'")

        with subtest("Service user and security"):
            machine.succeed("systemctl show mctomqtt.service | grep -q 'User=mctomqtt'")
            machine.succeed("systemctl show mctomqtt.service | grep -q 'Group=mctomqtt'")
            machine.succeed("systemctl show mctomqtt.service | grep -q 'Restart=on-failure'")

        with subtest("Service restart"):
            machine.succeed("systemctl restart mctomqtt.service")
            machine.wait_for_unit("mctomqtt.service")
            machine.succeed("systemctl is-active --quiet mctomqtt.service")

        print("All tests passed!")
      '';
    };
  };
}
