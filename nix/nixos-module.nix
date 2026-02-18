{self, ...}: {
  flake.nixosModules.default = {
    config,
    lib,
    pkgs,
    ...
  }: let
    cfg = config.services.mctomqtt;

    tomlFormat = pkgs.formats.toml {};

    # Map old flat broker attrs to the new nested TOML structure
    mapBroker = name: broker: let
      useTls = broker.use-tls or false;
      tlsVerify = broker.tls-verify or true;
      useAuthToken = broker.use-auth-token or false;
      authMethod =
        if useAuthToken
        then "token"
        else if (broker ? username)
        then "password"
        else "none";
    in
      {
        name = name;
        enabled = broker.enabled or true;
        server = broker.server;
        port = broker.port or 1883;
        transport = broker.transport or "tcp";
        keepalive = broker.keepalive or 60;
        qos = broker.qos or 0;
        retain = broker.retain or true;
        tls = {
          enabled = useTls;
          verify = tlsVerify;
        };
        auth =
          {
            method = authMethod;
          }
          // lib.optionalAttrs (broker ? username) {
            username = broker.username;
          }
          // lib.optionalAttrs (broker ? password) {
            password = broker.password;
          }
          // lib.optionalAttrs (broker ? token-audience) {
            audience = broker.token-audience;
          }
          // lib.optionalAttrs (broker ? owner) {
            owner = broker.owner;
          }
          // lib.optionalAttrs (broker ? email) {
            email = broker.email;
          };
      }
      // lib.optionalAttrs (broker ? client-id-prefix) {
        client_id_prefix = broker.client-id-prefix;
      };

    # Default broker configurations
    letsmeshUsBroker = mapBroker "letsmesh-us" {
      enabled = true;
      server = "mqtt-us-v1.letsmesh.net";
      port = 443;
      transport = "websockets";
      use-tls = true;
      use-auth-token = true;
      token-audience = "mqtt-us-v1.letsmesh.net";
    };

    letsmeshEuBroker = mapBroker "letsmesh-eu" {
      enabled = true;
      server = "mqtt-eu-v1.letsmesh.net";
      port = 443;
      transport = "websockets";
      use-tls = true;
      use-auth-token = true;
      token-audience = "mqtt-eu-v1.letsmesh.net";
    };

    # Combine default brokers with user-defined brokers
    allBrokers =
      lib.optional (cfg.defaults.letsmesh-us.enable) letsmeshUsBroker
      ++ lib.optional (cfg.defaults.letsmesh-eu.enable) letsmeshEuBroker
      ++ lib.imap1 (i: b: mapBroker (b.name or "broker-${toString i}") b) cfg.brokers;

    # Extract known settings from cfg.settings
    logLevel = cfg.settings.log-level or "INFO";
    syncTime = cfg.settings.sync-time or true;
    serialBaudRate = cfg.settings.serial-baud-rate or 115200;
    serialTimeout = cfg.settings.serial-timeout or 2;

    # Build the full TOML config structure
    tomlConfig = {
      general = {
        iata = cfg.iata;
        log_level = logLevel;
        sync_time = syncTime;
      };

      serial = {
        ports = cfg.serialPorts;
        baud_rate = serialBaudRate;
        timeout = serialTimeout;
      };

      topics = {
        status = "meshcore/{IATA}/{PUBLIC_KEY}/status";
        packets = "meshcore/{IATA}/{PUBLIC_KEY}/packets";
        debug = "meshcore/{IATA}/{PUBLIC_KEY}/debug";
        raw = "meshcore/{IATA}/{PUBLIC_KEY}/raw";
      };

      remote_serial = {
        enabled = false;
        allowed_companions = [];
      };

      update = {
        repo = "Cisien/meshcoretomqtt";
        branch = "main";
      };

      broker = allBrokers;
    };

    configFile = tomlFormat.generate "mctomqtt-config.toml" tomlConfig;
  in {
    options.services.mctomqtt = {
      enable = lib.mkEnableOption "MeshCore to MQTT bridge service";

      package = lib.mkOption {
        type = lib.types.package;
        default = self.packages.${pkgs.system}.default;
        defaultText = lib.literalExpression "self.packages.${pkgs.system}.default";
        description = "mctomqtt package to use";
      };

      iata = lib.mkOption {
        type = lib.types.strMatching "^[A-Z]{3}$";
        example = "XXX";
        description = "Three letter IATA code for geographic region";
      };

      serialPorts = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = ["/dev/ttyACM0"];
        description = "Serial ports to listen on (will be available to the mctomqtt user)";
        example = ["/dev/ttyACM0" "/dev/ttyACM1"];
      };

      brokers = lib.mkOption {
        type = lib.types.listOf (lib.types.attrsOf lib.types.anything);
        default = [];
        description = "List of MQTT broker configurations (appended after default brokers)";
        example = lib.literalExpression ''
          [
            {
              name = "my-broker";
              enabled = true;
              server = "mqtt.example.com";
              port = 1883;
              transport = "tcp";
              use-tls = false;
              tls-verify = true;
              client-id-prefix = "meshcore_";
              qos = 0;
              retain = true;
              keepalive = 60;
              username = "user";
              password = "pass";
            }
          ]
        '';
      };

      settings = lib.mkOption {
        type = lib.types.attrsOf lib.types.anything;
        default = {};
        description = "Additional settings for the TOML configuration";
        example = lib.literalExpression ''
          {
            serial-baud-rate = 115200;
            serial-timeout = 2;
            log-level = "INFO";
          }
        '';
      };

      defaults = {
        letsmesh-us = {
          enable = lib.mkOption {
            type = lib.types.bool;
            default = true;
            description = "Enable the LetsMesh US broker (mqtt-us-v1.letsmesh.net)";
          };
        };
        letsmesh-eu = {
          enable = lib.mkOption {
            type = lib.types.bool;
            default = true;
            description = "Enable the LetsMesh EU broker (mqtt-eu-v1.letsmesh.net)";
          };
        };
      };
    };

    config = lib.mkIf cfg.enable {
      assertions = [
        {
          assertion = cfg.package != null;
          message = "mctomqtt package not found. Either set services.mctomqtt.package or ensure the flake outputs a package for system ${pkgs.system}";
        }
      ];
      # Create system user and group
      users.users.mctomqtt = {
        isSystemUser = true;
        group = "mctomqtt";
        description = "MeshCore to MQTT bridge service user";
        extraGroups = ["dialout"]; # For serial port access
      };

      users.groups.mctomqtt = {};

      systemd.services.mctomqtt = {
        description = "MeshCore to MQTT Bridge";
        wantedBy = ["multi-user.target"];

        serviceConfig = {
          Type = "simple";
          ExecStart = "${cfg.package}/bin/mctomqtt --config ${configFile}";
          Restart = "on-failure";

          # Run as dedicated user
          User = "mctomqtt";
          Group = "mctomqtt";

          # Runtime directories
          StateDirectory = "mctomqtt";
          CacheDirectory = "mctomqtt";
          LogsDirectory = "mctomqtt";

          # Security settings
          NoNewPrivileges = true;
          PrivateTmp = true;
          ProtectSystem = "strict";
          ProtectHome = true;
          ReadWritePaths =
            [
              "/var/lib/mctomqtt"
              "/var/cache/mctomqtt"
              "/var/log/mctomqtt"
            ]
            ++ cfg.serialPorts;
        };

        # Ensure serial devices are available
        requires = map (port: "dev-${lib.replaceStrings ["/dev/"] [""] port}.device") cfg.serialPorts;
        after = ["network.target"] ++ map (port: "dev-${lib.replaceStrings ["/dev/"] [""] port}.device") cfg.serialPorts;
      };
    };
  };
}
