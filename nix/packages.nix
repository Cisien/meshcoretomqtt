{self, lib, ...}: {
  imports = [
  ];
  perSystem = {
    pkgs,
    self',
    ...
  }: let
    version-info = pkgs.writeTextFile {
      name = "version-info";
      destination = "/.version_info";
      text = builtins.toJSON {
        installer_version = "Nix package build";
        git_hash = self.ref or "unknown";
        install_date = "unknown";
      };
    };
    ed25519-orlp = pkgs.python313.pkgs.buildPythonApplication (finalAttrs: {
      pname = "ed25519-orlp";
      version = "0.1.3";
      pyproject = true;

      src = pkgs.fetchPypi {
        pname = "ed25519_orlp";
        inherit (finalAttrs) version;
        hash = "sha256-XfpS6PY2h/Ao7Q4/jis4W//SKmMMdAvmQtc6Uy0BN90=";
      };

      build-system = [
        pkgs.python313.pkgs.setuptools
      ];

      pythonImportsCheck = [
        "ed25519_orlp"
      ];

      meta = {
        description = "Python wrapper around Orson Peters' Ed25519 implementation";
        homepage = "https://pypi.org/project/ed25519-orlp";
        license = lib.licenses.zlib;
      };
    });
  in {
    # Package definitions
    packages.default = pkgs.python313.pkgs.buildPythonPackage {
      name = "mctomqtt";
      src = ./..;
      format = "other"; # Since we have no setup.py/pyproject.toml

      propagatedBuildInputs = with pkgs.python313Packages; [
        paho-mqtt
        pyserial
        ed25519-orlp
      ];

      nativeBuildInputs = [
        pkgs.makeWrapper
      ];

      installPhase = ''
        # Install Python files as modules
        mkdir -p $out/${pkgs.python313.sitePackages}
        install -Dm755 mctomqtt.py $out/${pkgs.python313.sitePackages}/mctomqtt.py
        install -Dm755 auth_token.py $out/${pkgs.python313.sitePackages}/auth_token.py
        install -Dm644 config_loader.py $out/${pkgs.python313.sitePackages}/config_loader.py
        # Copy the bridge package
        mkdir -p $out/${pkgs.python313.sitePackages}/bridge
        cp bridge/*.py $out/${pkgs.python313.sitePackages}/bridge/
        # Copy the pre-generated version info file
        install -Dm644 ${version-info}/.version_info $out/${pkgs.python313.sitePackages}/.version_info


        # Create executable wrapper for mctomqtt
        mkdir -p $out/bin
        makeWrapper ${pkgs.python313.interpreter} $out/bin/mctomqtt \
          --add-flags "$out/${pkgs.python313.sitePackages}/mctomqtt.py" \
          --set PYTHONPATH "$out/${pkgs.python313.sitePackages}:${pkgs.python313.withPackages (ps: with ps; [paho-mqtt pyserial ed25519-orlp])}/${pkgs.python313.sitePackages}"
      '';

      meta = {
        description = "A Python-based script to send MeshCore debug and packet capture data to MQTT for analysis.";
        mainProgram = "meshcoretomqtt";
        license = pkgs.lib.licenses.mit;
        homepage = "https://github.com/Cisien/meshcoretomqtt";
      };
    };
  };
}
