{self, ...}: {
  imports = [
  ];
  perSystem = {
    pkgs,
    self',
    ...
  }: {
    # Development shell
    devShells.default = pkgs.mkShell {
      packages = with pkgs; [
        (python313.withPackages (ps:
          with ps; [
            paho-mqtt
            pyserial
            ed25519-orlp
          ]))
      ];
    };
  };
}
