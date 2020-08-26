import typer
from pathlib import Path
from bionic.train import Trainer

app = typer.Typer()


@app.command("bionic")
def train(config_path: Path):
    # train(config_path)
    trainer = Trainer(config_path)
    trainer.train()
    trainer.forward()


def main():
    app()
